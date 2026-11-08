"""#263: USSD menus act through the same services as the web, in screens of at most 160 characters.

Requests are shaped like the ones Africa's Talking sent in discovery
(tests/fixtures/africastalking/ussd/).
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from app.db.base import Base
from app.models.transaction import TransactionStatus
from app.models.ussd_session import UssdSession
from app.services import transaction as transaction_service
from app.services import ussd as ussd_module
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from app.services.transaction import (
    apply_seller_resolution_action,
    confirm_payment_callback,
    dispatch_transaction,
    mark_transaction_arrived,
)
from app.services.ussd import MAX_SCREEN, handle_ussd, set_ussd_pin
from sqlalchemy import Table

from tests.test_anchoring_batcher import _db
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _open_and_fund_escrow,
    _seed_transaction_context,
)

FIXTURES = Path(__file__).parent / "fixtures" / "africastalking" / "ussd"
CODE = "246810"
PIN = "4321"
_sessions = itertools.count(1)
SCREENS: list[str] = []


def _ussd(db, phone: str, text: str, session: str | None = None, now: datetime | None = None):
    reply = handle_ussd(
        db,
        session_id=session or f"ATUid_test_{next(_sessions)}",
        phone_number=phone,
        text=text,
        now=now,
    )
    SCREENS.append(reply.text)
    return reply


def _with_tables():
    db = _db()
    Base.metadata.create_all(db.get_bind(), tables=[cast(Table, UssdSession.__table__)])
    return db


def _order(db, monkeypatch, suffix: str, *, serialized: bool, until: str):
    """An order moved through the real services to the wanted state, with Kenyan phones."""
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=serialized, suffix=suffix
    )
    buyer.phone, seller.phone = f"+2547111000{suffix}", f"+2547222000{suffix}"
    db.commit()
    for user in (buyer, seller):
        set_ussd_pin(db, user, PIN)
    if until == "awaiting_payment":
        return buyer, seller, transaction
    provider = SimulatedCustodyProvider(
        db, collection_rail=SimulatedRail(), payout_rail=SimulatedRail()
    )
    _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    if until == "locked":
        return buyer, seller, transaction
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    if until == "out_for_delivery":
        return buyer, seller, transaction
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: CODE)
    mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
    return buyer, seller, transaction


def _walk(db, phone: str, *steps: str):
    """One session: dial, then each answer, as AT sends them (text joined with *)."""
    session = f"ATUid_walk_{next(_sessions)}"
    replies = [_ussd(db, phone, "", session)]
    for i in range(1, len(steps) + 1):
        replies.append(_ussd(db, phone, "*".join(steps[:i]), session))
    return replies


@pytest.fixture(autouse=True, scope="module")
def _every_screen_fits():
    yield
    too_long = [s for s in SCREENS if len(s) > MAX_SCREEN]
    assert SCREENS and not too_long, too_long


def test_the_observed_request_shape_is_what_the_handler_reads() -> None:
    first = json.loads((FIXTURES / "dial.json").read_text())["request"]
    assert first["content_type"] == "application/x-www-form-urlencoded"
    assert set(first["form"]) == {"sessionId", "serviceCode", "phoneNumber", "networkCode", "text"}
    later = json.loads((FIXTURES / "free_text.json").read_text())["request"]["form"]
    assert later["text"] == "1*hello" and later["sessionId"] == first["form"]["sessionId"]


def test_an_unknown_number_is_told_to_sign_up() -> None:
    db = _with_tables()
    reply = _ussd(db, "+254799999999", "")
    assert reply.text.startswith("END ") and "no Vekro account" in reply.text


def test_buyer_accepts_at_the_door_with_the_delivery_code(monkeypatch) -> None:
    db = _with_tables()
    buyer, _seller, transaction = _order(db, monkeypatch, "01", serialized=False, until="door")

    menu, orders, order, prompt, done = _walk(db, buyer.phone, "1", "1", "1", CODE)

    assert menu.text.startswith("CON Vekro\n1 My orders")
    assert transaction.reference in orders.text and "At the door" in orders.text
    assert "1 Accept (enter code)" in order.text
    assert prompt.text.startswith("CON Enter the 6-digit delivery code")
    assert done.text == "END Accepted. Payment goes to the seller."
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.RELEASED


def test_a_wrong_delivery_code_changes_nothing(monkeypatch) -> None:
    db = _with_tables()
    buyer, _seller, transaction = _order(db, monkeypatch, "02", serialized=False, until="door")

    *_, done = _walk(db, buyer.phone, "1", "1", "1", "000000")

    assert done.text == "END Wrong delivery code. Nothing changed."
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AT_DOOR_PENDING_INSPECTION


def test_buyer_rejects_at_the_door_with_a_reason_and_pin(monkeypatch) -> None:
    db = _with_tables()
    buyer, _seller, transaction = _order(db, monkeypatch, "03", serialized=False, until="door")

    *_, reasons, pin, done = _walk(db, buyer.phone, "1", "1", "2", "2", PIN)

    assert "2 Wrong item" in reasons.text
    assert pin.text == "CON Enter your USSD PIN to reject"
    assert done.text.startswith("END Rejected.")
    db.refresh(transaction)
    assert transaction.status != TransactionStatus.AT_DOOR_PENDING_INSPECTION
    assert transaction.door_rejection_reason == "wrong_item"


def test_serialized_item_report_send_back_and_reconfirm(monkeypatch) -> None:
    db = _with_tables()
    buyer, seller, transaction = _order(db, monkeypatch, "04", serialized=True, until="door")
    *_, accepted = _walk(db, buyer.phone, "1", "1", "1", CODE)
    assert accepted.text.startswith("END Accepted. Payment is held 24 hours")

    *_, categories, _pin, reported = _walk(db, buyer.phone, "1", "1", "1", "1", PIN)
    assert "1 Not working" in categories.text and reported.text.startswith("END Reported.")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.DISPUTED_FUNCTIONAL

    *_, sent = _walk(db, buyer.phone, "1", "1", "1", PIN)
    assert sent.text.startswith("END Noted.")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.RETURN_IN_TRANSIT

    *_, received = _walk(db, seller.phone, "1", "1", "1", PIN)
    assert received.text.startswith("END Noted.")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.RETURN_RECEIVED

    apply_seller_resolution_action(db, transaction.id, seller, action="repair_shipped")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_BUYER_RECONFIRMATION
    *_, question, _pin, answered = _walk(db, buyer.phone, "1", "1", "1", PIN)
    assert "1 Yes, it's fixed" in question.text
    assert answered.text == "END Thanks. Payment goes to the seller."


def test_seller_dispatches_then_marks_arrived(monkeypatch) -> None:
    db = _with_tables()
    _buyer, seller, transaction = _order(db, monkeypatch, "05", serialized=False, until="locked")

    menu, _orders, order, _pin, done = _walk(db, seller.phone, "1", "1", "1", PIN)
    assert menu.text.startswith("CON Vekro seller")
    assert "1 Mark dispatched" in order.text and done.text.startswith("END Marked dispatched")

    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: CODE)
    *_, arrived = _walk(db, seller.phone, "1", "1", "1", PIN)
    assert arrived.text.startswith("END Marked arrived")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AT_DOOR_PENDING_INSPECTION


def test_a_wrong_pin_changes_nothing_and_three_lock_ussd(monkeypatch) -> None:
    db = _with_tables()
    _buyer, seller, transaction = _order(db, monkeypatch, "06", serialized=False, until="locked")

    for _ in range(3):
        *_, refused = _walk(db, seller.phone, "1", "1", "1", "0000")
        assert refused.text == "END Wrong PIN. Nothing changed."
    *_, locked = _walk(db, seller.phone, "1", "1", "1", PIN)
    assert locked.text.startswith("END Too many wrong PINs")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED


def test_without_a_pin_the_user_is_sent_to_set_one(monkeypatch) -> None:
    db = _with_tables()
    _buyer, seller, _transaction = _order(db, monkeypatch, "07", serialized=False, until="locked")
    seller.ussd_pin_hash = None
    db.commit()

    *_, refused = _walk(db, seller.phone, "1", "1", "1", PIN)
    assert refused.text.startswith("END Set your USSD PIN")


def test_an_expired_session_says_so() -> None:
    db = _with_tables()
    start = datetime.now(UTC)
    _ussd(db, "+254799999998", "", "ATUid_expiring", now=start)
    later = _ussd(db, "+254799999998", "1", "ATUid_expiring", now=start + timedelta(minutes=4))
    assert later.text == "END This session has expired. Please dial again."


def test_an_action_not_allowed_now_explains_and_a_stale_menu_is_refused(monkeypatch) -> None:
    db = _with_tables()
    _buyer, seller, transaction = _order(db, monkeypatch, "08", serialized=False, until="locked")
    session = "ATUid_stale"
    _ussd(db, seller.phone, "", session)
    _ussd(db, seller.phone, "1", session)
    shown = _ussd(db, seller.phone, "1*1", session)
    assert "1 Mark dispatched" in shown.text

    # Meanwhile the seller dispatches it on the web: the USSD menu is now stale.
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)

    stale = _ussd(db, seller.phone, "1*1*1", session)
    assert stale.text.startswith(f"END {transaction.reference} just changed. It is now: On the way")
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.OUT_FOR_DELIVERY


def test_the_web_and_ussd_racing_on_one_order_change_it_once(monkeypatch) -> None:
    db = _with_tables()
    buyer, _seller, transaction = _order(db, monkeypatch, "09", serialized=False, until="door")
    session = "ATUid_race"
    for text in ("", "1", "1*1", "1*1*1"):
        _ussd(db, buyer.phone, text, session)
    # The web accepts first; the USSD code entry then finds the order already moved on.
    transaction_service.confirm_buyer_delivery_otp(
        db, transaction_id=transaction.id, buyer=buyer, otp_code=CODE
    )
    late = _ussd(db, buyer.phone, f"1*1*1*{CODE}", session)
    assert late.text.startswith("END ") and "just changed" in late.text
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.RELEASED


def test_buyer_pays_a_pending_order_by_reference_after_the_reply(monkeypatch) -> None:
    db = _with_tables()
    buyer, _seller, transaction = _order(
        db, monkeypatch, "10", serialized=False, until="awaiting_payment"
    )
    sent: list[Any] = []
    monkeypatch.setattr(ussd_module, "_send_prompt", lambda *args: sent.append(args))
    typed = transaction.reference.replace("-", "").lower()

    _menu, ask, confirm, done = _walk(db, buyer.phone, "2", typed, PIN)

    assert ask.text.startswith("CON Enter the order reference")
    assert transaction.reference in confirm.text and "Enter your USSD PIN" in confirm.text
    assert done.text.startswith("END An M-Pesa prompt is coming") and sent == []
    done.after()  # what the router runs once AT has its answer
    assert sent == [(transaction.id, buyer.id)]


def test_set_ussd_pin_wants_four_digits() -> None:
    db = _with_tables()
    from app.services.ussd import UssdPinError

    with pytest.raises(UssdPinError):
        set_ussd_pin(db, object(), "12a4")  # type: ignore[arg-type]


# The HTTP layer: token, optional allowlist, AT's form encoding, plain-text reply.
from app.core.settings import get_settings  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

TOKEN = "test-ussd-token-0123456789"


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[cast(Table, User.__table__), cast(Table, UssdSession.__table__)]
    )
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    app.dependency_overrides[get_db] = lambda: db

    def configure(**overrides):
        settings = get_settings().model_copy(
            update={"ussd_callback_token": SecretStr(TOKEN), **overrides}
        )
        monkeypatch.setattr("app.routers.ussd.get_settings", lambda: settings)

    configure()
    yield TestClient(app), configure
    app.dependency_overrides.clear()


FORM = {
    "sessionId": "ATUid_router",
    "serviceCode": "*384*83579#",
    "phoneNumber": "+254799999997",
    "networkCode": "99999",
    "text": "",
}


def test_router_answers_at_form_posts_in_plain_text(client) -> None:
    http, _configure = client
    response = http.post(f"/ussd/africastalking/{TOKEN}", data=FORM)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("END ") and "no Vekro account" in response.text
    assert response.headers["at-ussd-hop-metadata"] == "no_account"


def test_router_refuses_a_wrong_token_or_when_off(client) -> None:
    http, configure = client
    assert http.post("/ussd/africastalking/wrong", data=FORM).status_code == 404
    configure(ussd_callback_token=SecretStr(""))
    assert http.post(f"/ussd/africastalking/{TOKEN}", data=FORM).status_code == 404


def test_router_allowlist_is_optional_and_reads_forwarded_for_only_when_trusted(client) -> None:
    http, configure = client
    url = f"/ussd/africastalking/{TOKEN}"
    forwarded = {"X-Forwarded-For": "35.177.236.113"}
    configure(ussd_allowed_ips="35.177.236.113")
    assert http.post(url, data=FORM, headers=forwarded).status_code == 404  # not trusted
    configure(ussd_allowed_ips="35.177.236.113", ussd_trust_forwarded_for=True)
    assert http.post(url, data=FORM, headers=forwarded).status_code == 200
    assert http.post(url, data=FORM, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404


def test_setting_the_ussd_pin_needs_the_password(monkeypatch) -> None:
    from app.core.security import hash_password, verify_password
    from app.routers.account import UssdPinRequest, post_ussd_pin
    from fastapi import HTTPException

    db = _with_tables()
    _buyer, seller, _transaction = _order(db, monkeypatch, "11", serialized=False, until="locked")
    seller.password_hash = hash_password("Mango2026")
    db.commit()

    with pytest.raises(HTTPException) as refused:
        post_ussd_pin(UssdPinRequest(pin="9876", password="wrong"), seller, db)
    assert refused.value.status_code == 403
    assert (
        post_ussd_pin(UssdPinRequest(pin="9876", password="Mango2026"), seller, db).status_code
        == 204
    )
    assert verify_password("9876", seller.ussd_pin_hash)
