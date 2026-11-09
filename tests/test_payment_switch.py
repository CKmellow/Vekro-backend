"""#280: pay by card instead on an unpaid M-Pesa order.

Allowed only while the order awaits payment and no M-Pesa prompt is open. Once switched the order
is funded by card only: a late M-Pesa payment is audited once for a manual refund, whether it
lands before or after the card payment, and the order is funded exactly once.
"""

# ruff: noqa: F811  (the db and world fixtures are imported, then requested by name)
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.anchor_event import AnchorEvent
from app.models.payment_prompt import PaymentPrompt, PaymentPromptStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.services import payment as payment_module
from app.services.card_payments import handle_ipn
from app.services.daraja_confirmation import confirm_pending_payments, handle_callback
from app.services.payment_prompts import record_payment_prompt
from app.services.payment_switch import (
    LATE_MPESA,
    SWITCHED,
    SwitchInitiationError,
    SwitchInvalidStateError,
    SwitchNotFoundError,
    SwitchPromptOpenError,
    SwitchUnavailableError,
    switch_to_card,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_anchoring_batcher import _db
from tests.test_card_payments import FakePesapal, _audits, _funding, _ipn, card_settings
from tests.test_daraja import CHECKOUT, FakeDaraja, _callback
from tests.test_simulated_custody_full_flow import _seed_transaction_context

PAID = (200, {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."})
CANCELLED = (200, {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"})
PROCESSING = (500, {"errorCode": "500.001.1001", "errorMessage": "processing"})


def _mpesa_order(
    db: Session, suffix: str, *, prompt_age: timedelta | None = timedelta(minutes=2)
) -> tuple[User, Transaction, PaymentPrompt | None]:
    buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    transaction.payment_method = "mpesa"
    transaction.collection_rail = "daraja"
    transaction.payment_mode = "sandbox"
    db.commit()
    prompt = None
    if prompt_age is not None:
        prompt = record_payment_prompt(
            db,
            transaction,
            payment_response=payment_module.PaymentResponse(
                provider="mpesa_daraja",
                status="queued",
                provider_request_id="m1",
                merchant_request_id="m1",
                checkout_request_id=f"{CHECKOUT}-{suffix}",
                customer_message="ok",
            ),
            now=datetime.now(UTC) - prompt_age,
        )
    return buyer, transaction, prompt


def _switch(db: Session, transaction: Transaction, buyer: User, **kwargs: Any) -> PaymentPrompt:
    kwargs.setdefault("settings", card_settings())
    kwargs.setdefault("card_rail", FakePesapal())
    return switch_to_card(db, transaction_id=transaction.id, buyer=buyer, **kwargs)


def _daraja(answer: tuple[int, dict[str, Any]]) -> FakeDaraja:
    fake = FakeDaraja()
    fake.query = lambda: answer
    return fake


def test_switch_closes_the_dead_prompt_and_starts_a_card_checkout() -> None:
    db = _db()
    buyer, transaction, stk = _mpesa_order(db, "81")
    daraja = _daraja(CANCELLED)

    card = _switch(db, transaction, buyer, daraja_client=daraja.client())

    db.refresh(transaction)
    db.refresh(stk)
    assert (card.kind, card.status) == ("redirect", PaymentPromptStatus.PENDING.value)
    assert card.redirect_url.startswith("https://cybqa.pesapal.com/")
    assert stk.status == PaymentPromptStatus.FAILED.value
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert (transaction.payment_method, transaction.collection_rail) == ("card", "pesapal")
    assert (transaction.payment_mode, transaction.custody_provider) == ("sandbox", "simulated")
    (audit,) = _audits(db, SWITCHED)
    assert audit.actor_id == buyer.id
    assert audit.details["from_rail"] == "daraja"
    assert audit.details["to_method"] == "card"


def test_the_switch_is_anchored_without_personal_data() -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "82")
    _switch(db, transaction, buyer, daraja_client=_daraja(CANCELLED).client())

    (audit,) = _audits(db, SWITCHED)
    anchor = db.execute(
        select(AnchorEvent).where(AnchorEvent.source_id == str(audit.id))
    ).scalar_one()
    assert anchor.transaction_id == transaction.id
    payload = json.loads(anchor.payload)
    assert payload["action"] == SWITCHED
    assert buyer.phone not in anchor.payload


def test_an_open_prompt_refuses_the_switch_with_a_retry_after() -> None:
    db = _db()
    buyer, transaction, stk = _mpesa_order(db, "83", prompt_age=timedelta(seconds=30))
    daraja = _daraja(PAID)

    with pytest.raises(SwitchPromptOpenError) as refused:
        _switch(db, transaction, buyer, daraja_client=daraja.client())

    assert 50 <= refused.value.retry_after_seconds <= 61
    db.refresh(transaction)
    assert transaction.payment_method == "mpesa"
    assert stk.status == PaymentPromptStatus.PENDING.value
    assert daraja.requests == []  # not even queried: the prompt is still on the phone


def test_a_stale_prompt_that_was_paid_locks_the_order_and_refuses_the_switch() -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "84")

    with pytest.raises(SwitchInvalidStateError, match="already paid"):
        _switch(db, transaction, buyer, daraja_client=_daraja(PAID).client())

    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert transaction.payment_method == "mpesa"
    assert _audits(db, SWITCHED) == []
    with pytest.raises(SwitchInvalidStateError, match="not awaiting payment"):
        _switch(db, transaction, buyer)


def test_a_stale_prompt_mpesa_cannot_settle_yet_waits_then_gives_up_after_ten_minutes() -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "85", prompt_age=timedelta(minutes=3))
    with pytest.raises(SwitchPromptOpenError) as refused:
        _switch(db, transaction, buyer, daraja_client=_daraja(PROCESSING).client())
    assert refused.value.retry_after_seconds == 30

    buyer, old, stk = _mpesa_order(db, "86", prompt_age=timedelta(minutes=11))
    _switch(db, old, buyer, daraja_client=_daraja(PROCESSING).client())
    db.refresh(old)
    db.refresh(stk)
    assert old.payment_method == "card"
    assert (stk.status, stk.failure_reason) == ("failed", "timeout")


def test_a_simulated_prompt_is_closed_without_a_query() -> None:
    db = _db()
    buyer, transaction, stk = _mpesa_order(db, "87")
    transaction.collection_rail = "simulated"
    db.commit()

    _switch(db, transaction, buyer)

    db.refresh(stk)
    assert stk.status == PaymentPromptStatus.FAILED.value
    assert transaction.payment_method == "card"


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"payment_method": "card"}, SwitchInvalidStateError, "already paid by card"),
        ({"custody_provider": "econfirm"}, SwitchInvalidStateError, "its M-Pesa prompt"),
    ],
)
def test_refusals(change: dict[str, Any], error: type[Exception], message: str) -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "88", prompt_age=None)
    for field, value in change.items():
        setattr(transaction, field, value)
    db.commit()

    with pytest.raises(error, match=message):
        _switch(db, transaction, buyer)
    assert _audits(db, SWITCHED) == []


def test_card_not_configured_and_someone_elses_order() -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "89", prompt_age=None)
    with pytest.raises(SwitchUnavailableError):
        _switch(db, transaction, buyer, settings=card_settings(pesapal_enabled=False))

    other, _t, _p = _mpesa_order(db, "90", prompt_age=None)
    with pytest.raises(SwitchNotFoundError):
        _switch(db, transaction, other)


def test_pesapal_refusing_the_checkout_leaves_a_card_order_to_retry() -> None:
    db = _db()
    buyer, transaction, _stk = _mpesa_order(db, "91", prompt_age=None)
    refusing = FakePesapal()
    refusing.accept = False

    with pytest.raises(SwitchInitiationError) as failed:
        _switch(db, transaction, buyer, card_rail=refusing)

    assert failed.value.prompt.status == PaymentPromptStatus.FAILED_TO_INITIATE.value
    db.refresh(transaction)
    assert transaction.payment_method == "card"


def _switched_order(
    db: Session, suffix: str
) -> tuple[Transaction, PaymentPrompt, PaymentPrompt, FakePesapal]:
    buyer, transaction, stk = _mpesa_order(db, suffix)
    pesapal = FakePesapal()
    card = _switch(
        db, transaction, buyer, card_rail=pesapal, daraja_client=_daraja(CANCELLED).client()
    )
    pesapal.status = FakePesapal.completed(str(transaction.amount), reference=transaction.reference)
    return transaction, stk, card, pesapal


def test_late_mpesa_before_the_card_payment_is_audited_once_and_the_card_funds() -> None:
    db = _db()
    transaction, stk, card, pesapal = _switched_order(db, "92")
    late = _callback(stk.checkout_request_id or "", amount=float(transaction.amount))

    assert handle_callback(db, late, client=_daraja(PAID).client()) == "extra_payment"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert _funding(db, transaction) == []
    # A replayed callback is not audited twice.
    assert handle_callback(db, late, client=_daraja(PAID).client()) == "already_processed"
    (extra,) = _audits(db, LATE_MPESA)
    assert extra.rail_name == "daraja"
    assert extra.amount == transaction.amount

    assert handle_ipn(db, _ipn(transaction, card), rail=pesapal)["status"] == 200
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert len(_funding(db, transaction)) == 1
    (funded,) = _audits(db, "callback_funding_confirmed")
    assert funded.rail_name == "pesapal"


def test_late_mpesa_after_the_card_payment_is_audited_and_never_funds_twice() -> None:
    db = _db()
    transaction, stk, card, pesapal = _switched_order(db, "93")
    assert handle_ipn(db, _ipn(transaction, card), rail=pesapal)["status"] == 200

    late = _callback(stk.checkout_request_id or "", amount=float(transaction.amount))
    assert handle_callback(db, late, client=_daraja(PAID).client()) == "extra_payment"

    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert len(_funding(db, transaction)) == 1
    assert len(_audits(db, LATE_MPESA)) == 1
    assert len(_audits(db, "callback_funding_confirmed")) == 1


def test_a_late_mpesa_callback_that_was_not_paid_changes_nothing() -> None:
    db = _db()
    transaction, stk, _card, _pesapal = _switched_order(db, "94")
    late = _callback(stk.checkout_request_id or "", amount=float(transaction.amount))

    assert handle_callback(db, late, client=_daraja(CANCELLED).client()) == "failed"
    assert handle_callback(db, late, client=_daraja(PROCESSING).client()) == "pending"
    assert _audits(db, LATE_MPESA) == []
    # The watcher skips the closed STK prompt and never STK-queries the open card checkout.
    assert confirm_pending_payments(db, client=_daraja(PAID).client()) == {}


# --- the endpoint ---------------------------------------------------------------------------

from tests.test_card_payments_api import _card_api  # noqa: E402
from tests.test_payment_prompts import _client_as, db, world  # noqa: E402, F401


def test_api_switches_to_card_after_the_prompt_expires(monkeypatch, db, world) -> None:
    _card_api(monkeypatch, FakePesapal())
    monkeypatch.setattr("app.services.payment_switch.get_settings", card_settings)
    monkeypatch.setattr(
        "app.services.payment_switch.build_card_rail", lambda *_a, **_k: FakePesapal()
    )
    client = _client_as(monkeypatch, db, world["buyer"])
    created = client.post("/transactions", json={"listing_id": str(world["listing"].id)}).json()

    # Inside the prompt's lifetime: 409 with Retry-After.
    early = client.post(f"/transactions/{created['id']}/payment/card")
    assert early.status_code == 409
    assert int(early.headers["Retry-After"]) > 0

    # Expire the prompt (the stub gateway's prompt never completes on its own).
    for prompt in db.execute(select(PaymentPrompt)).scalars():
        prompt.sent_at = prompt.sent_at - timedelta(minutes=5)
        prompt.expires_at = prompt.expires_at - timedelta(minutes=5)
    db.commit()

    response = client.post(f"/transactions/{created['id']}/payment/card")
    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["status"]) == ("REDIRECT", "pending")
    assert body["redirect_url"].startswith("https://cybqa.pesapal.com/")

    again = client.post(f"/transactions/{created['id']}/payment/card")
    assert again.status_code == 409
    detail = client.get(f"/transactions/{created['id']}").json()
    assert detail["payment_method"] == "card"
    assert detail["payment"]["kind"] == "REDIRECT"


def test_api_is_503_without_card_payments(monkeypatch, db, world) -> None:
    _card_api(monkeypatch, FakePesapal())
    off = card_settings(pesapal_enabled=False)
    monkeypatch.setattr("app.services.payment_switch.get_settings", lambda: off)
    client = _client_as(monkeypatch, db, world["buyer"])
    created = client.post("/transactions", json={"listing_id": str(world["listing"].id)}).json()

    assert client.post(f"/transactions/{created['id']}/payment/card").status_code == 503
