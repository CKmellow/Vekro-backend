"""#251: card payments through Pesapal's hosted checkout.

The IPN and the buyer's return are triggers only. Funding is recorded after Vekro's own status
query reports COMPLETED for the exact amount, currency and merchant reference; everything else is
refused, audited (and anchored) or left waiting.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from app.core.settings import get_settings
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payment_prompt import PaymentPrompt, PaymentPromptStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.schemas.transaction import CreateTransactionRequest
from app.services import card_payments
from app.services.card_payments import (
    confirm_pending_card_payments,
    handle_ipn,
    refresh_card_payment,
    return_url,
)
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from app.services.payment_prompts import resend_payment_prompt
from app.services.transaction import PaymentInitiationError, create_transaction
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_anchoring_batcher import _db
from tests.test_simulated_custody_full_flow import _seed_transaction_context

CARD = {
    "pesapal_enabled": True,
    "pesapal_base_url": "https://cybqa.pesapal.com/pesapalv3",
    "pesapal_consumer_key": "demo-key",
    "pesapal_consumer_secret": "demo-secret",
    "pesapal_ipn_id": "ipn-1",
    "frontend_url": "https://app.example",
}


def card_settings(**overrides: Any) -> Any:
    return get_settings().model_copy(update={**CARD, **overrides})


class FakePesapal:
    """The two calls Vekro makes, answering like the sandbox (docs/pesapal-observed.md)."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.queries: list[str] = []
        self.accept = True
        self.status = self.completed("1.0")

    @staticmethod
    def completed(amount: str, currency: str = "KES", reference: str | None = None):
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference="track-1",
            raw_status="completed",
            metadata={
                "amount": amount,
                "currency": currency,
                "merchant_reference": reference,
                "confirmation_code": "CONF-1",
            },
        )

    @staticmethod
    def with_outcome(outcome: CollectionOutcome, raw: str):
        return CollectionResult(outcome=outcome, provider_reference="track-1", raw_status=raw)

    def request_funding(self, request):
        self.submitted.append(request)
        if not self.accept:
            return CollectionResult(
                outcome=CollectionOutcome.FAILED_DEFINITE,
                provider_reference="none",
                raw_status="rejected",
            )
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=f"track-{len(self.submitted)}",
            raw_status="200",
            metadata={"redirect_url": "https://cybqa.pesapal.com/pesapaliframe/x"},
        )

    def get_funding_status(self, tracking_id: str):
        self.queries.append(tracking_id)
        return self.status


def _card_order(db: Session, suffix: str, fake: FakePesapal) -> tuple[Transaction, PaymentPrompt]:
    buyer, _seller, listing, _unpaid = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    transaction = create_transaction(
        db,
        buyer,
        CreateTransactionRequest(listing_id=listing.id, payment_method="card"),
        settings=card_settings(),
        card_rail=fake,
    )
    prompt = db.execute(
        select(PaymentPrompt).where(PaymentPrompt.transaction_id == transaction.id)
    ).scalar_one()
    fake.status = FakePesapal.completed(str(transaction.amount), reference=transaction.reference)
    return transaction, prompt


def _audits(db: Session, action: str) -> list[MoneyAuditEvent]:
    return list(
        db.execute(select(MoneyAuditEvent).where(MoneyAuditEvent.action == action)).scalars()
    )


def _funding(db: Session, transaction: Transaction) -> list[LedgerEntry]:
    return [
        entry
        for entry in db.execute(select(LedgerEntry)).scalars()
        if entry.account_code == "ESCROW_HELD"
        and entry.idempotency_key.endswith(str(transaction.id))
    ]


def test_a_card_order_gets_a_redirect_to_pesapal_and_the_return_page() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "51", fake)

    assert transaction.payment_method == "card"
    assert transaction.custody_provider == "simulated"
    assert (prompt.kind, prompt.status) == ("redirect", "pending")
    assert prompt.redirect_url.startswith("https://cybqa.pesapal.com/")
    assert fake.submitted[0].account_reference == transaction.reference
    assert return_url(card_settings(), transaction) == (
        f"https://app.example/pay/return?order={transaction.id}&ref={transaction.reference}"
    )


def test_card_is_refused_when_not_configured() -> None:
    db = _db()
    buyer, _seller, listing, _ = _seed_transaction_context(
        db, serialized_listing=False, suffix="52"
    )
    with pytest.raises(Exception, match="Card payments are not available"):
        create_transaction(
            db,
            buyer,
            CreateTransactionRequest(listing_id=listing.id, payment_method="card"),
            settings=card_settings(pesapal_ipn_id=""),
            card_rail=FakePesapal(),
        )


def test_pesapal_refusing_the_order_is_a_failed_initiation() -> None:
    db, fake = _db(), FakePesapal()
    fake.accept = False
    buyer, _seller, listing, _ = _seed_transaction_context(
        db, serialized_listing=False, suffix="53"
    )
    with pytest.raises(PaymentInitiationError) as error:
        create_transaction(
            db,
            buyer,
            CreateTransactionRequest(listing_id=listing.id, payment_method="card"),
            settings=card_settings(),
            card_rail=fake,
        )
    prompt = db.execute(
        select(PaymentPrompt).where(PaymentPrompt.transaction_id == error.value.transaction.id)
    ).scalar_one()
    assert prompt.status == PaymentPromptStatus.FAILED_TO_INITIATE.value


def _ipn(transaction: Transaction, prompt: PaymentPrompt) -> dict[str, Any]:
    return {
        "OrderTrackingId": prompt.checkout_request_id,
        "OrderMerchantReference": transaction.reference,
        "OrderNotificationType": "IPNCHANGE",
    }


def test_a_completed_payment_funds_the_order_and_a_replayed_ipn_changes_nothing() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "54", fake)

    answer = handle_ipn(db, _ipn(transaction, prompt), rail=fake)
    db.refresh(transaction)
    assert answer == {
        "orderNotificationType": "IPNCHANGE",
        "orderTrackingId": prompt.checkout_request_id,
        "orderMerchantReference": transaction.reference,
        "status": 200,
    }
    assert transaction.status == TransactionStatus.LOCKED
    (funded,) = _audits(db, "callback_funding_confirmed")
    assert funded.rail_name == "pesapal"
    assert len(_funding(db, transaction)) == 1

    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    assert len(_audits(db, "callback_funding_confirmed")) == 1
    assert len(_funding(db, transaction)) == 1
    assert len(fake.queries) == 1  # the replay did not even query


def test_an_ipn_for_an_unknown_order_is_refused() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "55", fake)

    unknown = handle_ipn(
        db, {"OrderTrackingId": "nobody", "OrderMerchantReference": "VK-NOPE00"}, rail=fake
    )
    wrong_reference = handle_ipn(
        db, {**_ipn(transaction, prompt), "OrderMerchantReference": "VK-OTHER0"}, rail=fake
    )

    assert unknown["status"] == wrong_reference["status"] == 500
    assert fake.queries == []
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT


@pytest.mark.parametrize(
    "change",
    [{"amount": "1.00"}, {"currency": "USD"}, {"reference": "VK-OTHER0"}],
    ids=["amount", "currency", "merchant reference"],
)
def test_a_completed_payment_that_does_not_match_is_refused_and_audited(change) -> None:
    db, fake = _db(), FakePesapal()
    transaction, _prompt = _card_order(db, "56", fake)
    paid = {"amount": str(transaction.amount), "reference": transaction.reference, **change}
    fake.status = FakePesapal.completed(
        paid["amount"], currency=paid.get("currency", "KES"), reference=paid["reference"]
    )

    assert refresh_card_payment(db, transaction, rail=fake) == "mismatch"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert _funding(db, transaction) == []
    (refused,) = _audits(db, "card_payment_mismatch")
    assert refused.transaction_id == transaction.id
    # Checking again (an IPN always re-checks) neither funds nor audits twice, and the
    # scheduler leaves it to support.
    assert refresh_card_payment(db, transaction, rail=fake) == "mismatch"
    assert len(_audits(db, "card_payment_mismatch")) == 1
    assert confirm_pending_card_payments(db, rail=fake) == {}
    assert _funding(db, transaction) == []


def test_an_unknown_status_changes_nothing() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "57", fake)
    fake.status = FakePesapal.with_outcome(CollectionOutcome.UNKNOWN, "payment_details_not_found")

    assert refresh_card_payment(db, transaction, rail=fake) == "pending"
    db.refresh(transaction)
    db.refresh(prompt)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert prompt.status == PaymentPromptStatus.PENDING.value
    assert db.execute(select(MoneyAuditEvent)).scalars().all() == []


def test_a_declined_card_closes_the_checkout_and_is_audited() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "58", fake)
    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")

    assert refresh_card_payment(db, transaction, rail=fake) == "failed"
    db.refresh(prompt)
    assert (prompt.status, prompt.failure_reason) == ("failed", "declined")
    assert len(_audits(db, "card_payment_failed")) == 1
    # Still declined on the next checks: recorded once.
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    assert confirm_pending_card_payments(db, rail=fake) == {transaction.id: "failed"}
    assert len(_audits(db, "card_payment_failed")) == 1


@pytest.mark.parametrize("trigger", ["ipn", "scheduler", "return page"])
def test_a_declined_card_retried_on_the_same_pesapal_order_funds_exactly_once(trigger) -> None:
    """#307 (VK-2N6HSK): Pesapal's "Try Again" reuses the tracking id after a decline."""
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, f"7{len(trigger)}", fake)
    paid = fake.status
    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    db.refresh(prompt)
    assert (prompt.status, prompt.failure_reason) == ("failed", "declined")

    fake.status = paid  # the second card went through on the same Pesapal order
    queries = len(fake.queries)
    if trigger == "ipn":
        assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    elif trigger == "scheduler":
        assert confirm_pending_card_payments(db, rail=fake) == {transaction.id: "confirmed"}
    else:
        assert refresh_card_payment(db, transaction, rail=fake) == "confirmed"
    assert fake.queries[queries:] == [prompt.checkout_request_id]

    db.refresh(transaction)
    db.refresh(prompt)
    assert transaction.status == TransactionStatus.LOCKED
    assert (prompt.status, prompt.failure_reason) == ("succeeded", None)
    assert len(_audits(db, "callback_funding_confirmed")) == 1
    assert len(_funding(db, transaction)) == 1

    # Every later trigger leaves it funded once.
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    assert confirm_pending_card_payments(db, rail=fake) == {}
    assert refresh_card_payment(db, transaction, rail=fake) == "already_processed"
    assert len(_audits(db, "callback_funding_confirmed")) == 1
    assert len(_funding(db, transaction)) == 1


def test_a_declined_checkout_whose_retry_does_not_match_is_refused() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "79", fake)
    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")
    handle_ipn(db, _ipn(transaction, prompt), rail=fake)
    fake.status = FakePesapal.completed("1.00", reference=transaction.reference)

    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert _funding(db, transaction) == []
    assert len(_audits(db, "card_payment_mismatch")) == 1


def test_a_declined_checkout_older_than_the_window_is_not_polled() -> None:
    from datetime import UTC, datetime, timedelta

    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "80", fake)
    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")
    handle_ipn(db, _ipn(transaction, prompt), rail=fake)
    later = datetime.now(UTC) + card_payments.RECONCILE_WINDOW + timedelta(minutes=1)
    assert confirm_pending_card_payments(db, rail=fake, now=later) == {}


def test_the_return_page_before_the_ipn_funds_once() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "59", fake)

    assert refresh_card_payment(db, transaction, rail=fake) == "confirmed"
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    assert confirm_pending_card_payments(db, rail=fake) == {}
    assert len(_audits(db, "callback_funding_confirmed")) == 1
    assert len(_funding(db, transaction)) == 1


def test_resend_keeps_an_open_checkout_and_replaces_only_a_failed_one() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "60", fake)
    buyer = db.get(User, transaction.buyer_id)
    same = resend_payment_prompt(
        db, transaction_id=transaction.id, buyer=buyer, settings=card_settings(), card_rail=fake
    )
    assert same.id == prompt.id and len(fake.submitted) == 1

    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")
    refresh_card_payment(db, transaction, rail=fake)
    fresh = resend_payment_prompt(
        db, transaction_id=transaction.id, buyer=buyer, settings=card_settings(), card_rail=fake
    )
    assert fresh.id != prompt.id and fresh.kind == "redirect" and len(fake.submitted) == 2


def test_card_money_facts_are_anchored_without_provider_ids() -> None:
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "61", fake)
    handle_ipn(db, _ipn(transaction, prompt), rail=fake)

    from app.models.anchor_event import AnchorEvent

    payloads = [
        json.loads(e.payload)
        for e in db.execute(
            select(AnchorEvent).where(AnchorEvent.transaction_id == transaction.id)
        ).scalars()
    ]
    actions = [p.get("action") for p in payloads if p["type"] == "money_audit"]
    assert "callback_funding_confirmed" in actions
    text = json.dumps(payloads)
    assert prompt.checkout_request_id not in text and "CONF-1" not in text


def test_card_amounts_compare_as_money() -> None:
    assert card_payments._decimal("1.0") == Decimal("1.00")
    assert card_payments._decimal("abc") is None


def test_the_checkout_sends_the_buyers_name_for_pesapals_billing_details() -> None:
    db, fake = _db(), FakePesapal()
    transaction, _prompt = _card_order(db, "59", fake)
    buyer = db.get(User, transaction.buyer_id)

    (submitted,) = fake.submitted
    assert submitted.payer_name == buyer.name
