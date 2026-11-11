"""#311: the presentation demo reset (app/services/demo_reset.py, `app.cli demo-reset`)."""

from __future__ import annotations

import pytest
from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction
from app.models.user import PartyRole, User, UserRole
from app.schemas.transaction import PaymentCallbackRequest
from app.services import transaction as tx
from app.services.demo_reset import (
    ASSETS,
    SHOP,
    STAGED,
    DemoResetRefusedError,
    run_demo_reset,
    series_phones,
    wait_until_paid,
)
from app.services.payment import MpesaStkStubGateway
from sqlalchemy import select

from tests.test_ussd import _with_tables as _ussd_tables


def _with_tables():
    from typing import cast

    from app.db.base import Base
    from sqlalchemy import Table

    db = _ussd_tables()
    Base.metadata.create_all(db.get_bind(), tables=[cast(Table, ListingPhoto.__table__)])
    return db


PAYER = "+254711000999"
YES = {"DEMO_RESET_CONFIRM": "yes"}


def _sandbox_pays(db, order: Transaction) -> bool:
    """Stands in for the buyer approving the sandbox prompt and Vekro confirming it."""
    tx.confirm_payment_callback(db, PaymentCallbackRequest(transaction_id=order.id, result_code=0))
    return True


def _reset(db, **kwargs):
    options = {
        "environ": YES,
        "payer_phone": PAYER,
        "payment_gateway": MpesaStkStubGateway(),
        "pay": _sandbox_pays,
        "sleep": lambda _seconds: None,
        "say": lambda _message: None,
    }
    return run_demo_reset(db, **(options | kwargs))


@pytest.mark.parametrize("environ", [{}, {"DEMO_RESET_CONFIRM": "no"}, {"DEMO_RESET_CONFIRM": ""}])
def test_it_refuses_without_the_explicit_confirmation(environ) -> None:
    db = _with_tables()
    with pytest.raises(DemoResetRefusedError, match="DEMO_RESET_CONFIRM=yes"):
        _reset(db, environ=environ)
    assert db.execute(select(User)).first() is None


def test_it_stages_every_state_through_the_real_services() -> None:
    db = _with_tables()
    result = _reset(db)

    assert {o.state: o.status for o in result.orders} == {
        "paid": "locked",
        "out_for_delivery": "out_for_delivery",
        "at_door": "at_door_pending_inspection",
        "hold_24h": "hold_24h",
        "dispute": "escalated_admin_review",
    }
    door = next(o for o in result.orders if o.state == "at_door")
    assert door.delivery_code and len(door.delivery_code) == 6


def test_new_accounts_on_a_fresh_series_hide_old_orders() -> None:
    db = _with_tables()
    result = _reset(db, series=4)
    phones = series_phones(4)
    assert {a.role: a.phone for a in result.accounts} == phones
    assert all(a.password for a in result.accounts)
    buyer = db.execute(select(User).where(User.phone == phones["buyer"])).scalar_one()
    # Prompts go to the payer phone; the login is the new demo number.
    assert buyer.mpesa_phone == PAYER and buyer.party_role == PartyRole.BUYER
    admin = db.execute(select(User).where(User.phone == phones["admin"])).scalar_one()
    assert admin.role == UserRole.ADMIN
    seller = db.execute(select(User).where(User.phone == phones["seller"])).scalar_one()
    assert seller.shop_name
    orders = db.execute(select(Transaction).where(Transaction.buyer_id == buyer.id)).scalars().all()
    assert len(orders) == len(STAGED)


def test_every_listing_has_a_photo_and_the_shop_is_made_once() -> None:
    db = _with_tables()
    first = _reset(db)
    assert len(first.shop_listings) == len(SHOP)
    listings = db.execute(select(Listing)).scalars().all()
    photos = {p.listing_id for p in db.execute(select(ListingPhoto)).scalars()}
    assert {listing.id for listing in listings} == photos
    for item in [*SHOP, *STAGED.values()]:
        assert (ASSETS / item.photo).is_file()

    again = _reset(db, states=("at_door",))
    assert again.shop_listings == []  # the shop is not duplicated
    assert all(a.password is None for a in again.accounts)  # accounts reused, passwords kept
    assert [o.state for o in again.orders] == ["at_door"]


def test_an_unpaid_prompt_is_skipped_never_marked_paid() -> None:
    db = _with_tables()
    result = _reset(db, states=("paid", "hold_24h"), pay=lambda _db, _order: False)
    assert [(o.status, o.note) for o in result.orders] == [
        ("awaiting_payment", "not paid: skipped"),
        ("awaiting_payment", "not paid: skipped"),
    ]


def test_it_waits_for_vekros_own_confirmation() -> None:
    db = _with_tables()
    result = _reset(db, states=("paid",))
    order = db.execute(
        select(Transaction).where(Transaction.reference == result.orders[0].reference)
    ).scalar_one()
    naps: list[float] = []
    assert wait_until_paid(db, order, sleep=naps.append) is True and naps == []

    pending = _reset(db, states=("paid",), pay=lambda _db, _order: False).orders[0]
    unpaid = db.execute(
        select(Transaction).where(Transaction.reference == pending.reference)
    ).scalar_one()
    naps.clear()
    assert (
        wait_until_paid(db, unpaid, timeout_seconds=20, poll_seconds=5, sleep=naps.append) is False
    )
    assert naps == [5, 5, 5, 5, 5]


def test_bad_options_are_refused() -> None:
    db = _with_tables()
    with pytest.raises(DemoResetRefusedError, match="Unknown state"):
        _reset(db, states=("paid", "teleported"))
    with pytest.raises(DemoResetRefusedError, match="series"):
        _reset(db, series=2)
    with pytest.raises(DemoResetRefusedError, match="payer"):
        _reset(db, payer_phone="12345")


def test_the_cli_keeps_passwords_out_of_its_output(tmp_path, monkeypatch, capsys) -> None:
    from app import cli

    db = _with_tables()
    monkeypatch.setattr(cli, "_session", lambda: _Ctx(db))
    monkeypatch.setattr("app.services.payment.get_payment_gateway", MpesaStkStubGateway)
    monkeypatch.setattr(
        "app.services.demo_reset.wait_until_paid", lambda db, order, **_: _sandbox_pays(db, order)
    )
    monkeypatch.setattr("app.services.demo_reset.time.sleep", lambda _s: None)
    logins = tmp_path / "logins.txt"

    monkeypatch.delenv("DEMO_RESET_CONFIRM", raising=False)
    assert cli.main(["demo-reset", "--payer-phone", PAYER, "--logins-file", str(logins)]) == 2

    monkeypatch.setenv("DEMO_RESET_CONFIRM", "yes")
    argv = ["demo-reset", "--payer-phone", PAYER, "--logins-file", str(logins), "--states", "paid"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    saved = logins.read_text()
    passwords = [
        line.split()[-1]
        for line in saved.splitlines()
        if line.startswith(("seller", "buyer", "admin", "card_buyer"))
    ]
    assert len(passwords) == 4
    assert not any(p in out for p in passwords)
    assert oct(logins.stat().st_mode & 0o777) == "0o600"


class _Ctx:
    def __init__(self, db) -> None:
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc) -> None:
        return None
