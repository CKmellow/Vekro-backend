"""#265: the USSD demo seed, and "My orders" showing what needs the caller first."""

from __future__ import annotations

import pytest
from app.services.ussd_demo import DemoSeedRefusedError, seed_ussd_demo

from tests.test_ussd import _walk, _with_tables

BUYER, SELLER = "+254711000123", "+254722000123"


def _seed(db, environment: str = "development"):
    return seed_ussd_demo(
        db, buyer_phone=BUYER, seller_phone=SELLER, pin="1234", environment=environment
    )


def test_the_seed_puts_orders_in_every_state_worth_showing() -> None:
    db = _with_tables()
    seed = _seed(db)

    states = [o.state for o in seed.orders]
    assert states == [
        "hold_24h",
        "at_door_pending_inspection",
        "out_for_delivery",
        "locked",
        "awaiting_payment",
    ]
    door = next(o for o in seed.orders if o.state == "at_door_pending_inspection")
    assert door.delivery_code and len(door.delivery_code) == 6
    assert seed.password  # new accounts get a printed web password


def test_both_demo_users_see_what_needs_them_first_and_can_act() -> None:
    db = _with_tables()
    seed = _seed(db)
    door = next(o for o in seed.orders if o.state == "at_door_pending_inspection")

    _menu, buyer_orders = _walk(db, BUYER, "1")[:2]
    assert door.reference in buyer_orders.text and "In the 24h check" in buyer_orders.text
    _menu, seller_orders = _walk(db, SELLER, "1")[:2]
    assert "Paid, waiting for dispatch" in seller_orders.text and "On the way" in seller_orders.text

    lines = buyer_orders.text.splitlines()[1:]
    door_line = next(i for i, line in enumerate(lines, 1) if door.reference in line)
    *_, accepted = _walk(db, BUYER, "1", str(door_line), "1", door.delivery_code)
    assert accepted.text == "END Accepted. Payment goes to the seller."


def test_reseeding_reuses_the_accounts() -> None:
    db = _with_tables()
    _seed(db)
    again = _seed(db)
    assert again.password is None and len(again.orders) == 5


def test_the_seed_never_runs_in_production() -> None:
    db = _with_tables()
    with pytest.raises(DemoSeedRefusedError):
        _seed(db, environment="production")
