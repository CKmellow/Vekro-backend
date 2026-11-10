"""Pay by card instead (#280): switch an unpaid M-Pesa order to a Pesapal card checkout.

The switch is allowed only while the order awaits payment and no M-Pesa prompt is open, so the
buyer cannot pay twice by accident. A prompt inside its lifetime refuses the switch; an older one
still marked pending is settled first by Vekro's own STK query (a paid one locks the order and the
switch is refused). Once switched, the order is funded by card only: an M-Pesa payment that still
lands is recorded for support to refund (``record_mpesa_after_card_switch``), never used to lock
the order, whether it arrives before or after the card payment.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payment_prompt import (
    PaymentFailureReason,
    PaymentPrompt,
    PaymentPromptKind,
    PaymentPromptStatus,
)
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.services.anchoring.provider_ref import candidate_refs, provider_ref
from app.services.audit import ACTOR_PROVIDER, ACTOR_USER, record_money_audit_event
from app.services.card_payments import (
    PAYMENT_METHOD_CARD,
    card_payments_available,
    start_card_checkout,
)
from app.services.card_payments import build_rail as build_card_rail
from app.services.custody.selection import PROVIDER_ECONFIRM, PROVIDER_SIMULATED
from app.services.payment_prompts import PROMPT_LIFETIME, latest_payment_prompt
from app.services.payment_rails import RAIL_DARAJA, RAIL_PESAPAL, set_collection

audit_logger = logging.getLogger("app.transactions.audit")

SWITCHED = "payment_method_switched"
LATE_MPESA = "mpesa_payment_after_card_switch"
# An STK prompt is long dead by then (the same bound as the Daraja watcher, #243).
STALE_PROMPT_AGE = timedelta(minutes=10)
# How long to ask the buyer to wait while a stale prompt cannot be settled yet.
QUERY_RETRY_SECONDS = 30


class SwitchNotFoundError(Exception):
    pass


class SwitchInvalidStateError(Exception):
    pass


class SwitchUnavailableError(Exception):
    pass


class SwitchPromptOpenError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "Your M-Pesa prompt is still open. Finish or cancel it on your phone, then try again."
        )
        self.retry_after_seconds = max(1, retry_after_seconds)


class SwitchInitiationError(Exception):
    def __init__(self, prompt: PaymentPrompt | None) -> None:
        super().__init__("Pesapal did not accept the card checkout.")
        self.prompt = prompt


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _pending_stk_prompts(db: Session, transaction_id: uuid.UUID) -> list[PaymentPrompt]:
    return list(
        db.execute(
            select(PaymentPrompt)
            .where(
                PaymentPrompt.transaction_id == transaction_id,
                PaymentPrompt.kind == PaymentPromptKind.STK_PROMPT.value,
                PaymentPrompt.status == PaymentPromptStatus.PENDING.value,
            )
            .order_by(PaymentPrompt.sent_at)
        ).scalars()
    )


def _settle_stale_prompts(
    db: Session,
    transaction: Transaction,
    *,
    settings: Any,
    daraja_client: Any,
    now: datetime,
) -> None:
    """Refuse while a prompt is open; settle older pending ones with an STK query first."""
    for prompt in _pending_stk_prompts(db, transaction.id):
        sent_at = _utc(prompt.sent_at)
        if now < sent_at + PROMPT_LIFETIME:
            raise SwitchPromptOpenError(int((sent_at + PROMPT_LIFETIME - now).total_seconds()) + 1)
        if transaction.collection_rail != RAIL_DARAJA or not prompt.checkout_request_id:
            continue  # simulated prompts never complete on their own; closed by the switch
        client = daraja_client
        if client is None and getattr(settings, "daraja_enabled", False):
            from app.services.daraja import DarajaClient

            client = DarajaClient.from_settings(settings)
        outcome = "unconfirmed"
        if client is not None:
            from app.services.daraja_confirmation import confirm_payment

            outcome = confirm_payment(db, transaction, prompt, client=client)
        if outcome == "confirmed":
            raise SwitchInvalidStateError("This order is already paid by M-Pesa.")
        if outcome in {"pending", "unconfirmed"} and now < sent_at + STALE_PROMPT_AGE:
            raise SwitchPromptOpenError(QUERY_RETRY_SECONDS)


def switch_to_card(
    db: Session,
    *,
    transaction_id: uuid.UUID,
    buyer: User,
    settings: Any = None,
    card_rail: Any = None,
    daraja_client: Any = None,
    now: datetime | None = None,
) -> PaymentPrompt:
    """Move an unpaid M-Pesa order to a Pesapal card checkout and return the REDIRECT prompt."""
    current = now or datetime.now(UTC)
    settings = settings or get_settings()

    transaction = db.get(Transaction, transaction_id)
    if transaction is None or transaction.buyer_id != buyer.id:
        raise SwitchNotFoundError("Transaction not found.")
    _check_switchable(transaction, settings)

    _settle_stale_prompts(
        db, transaction, settings=settings, daraja_client=daraja_client, now=current
    )

    # Lock the order, then re-check: a callback may have paid it meanwhile.
    db.commit()
    transaction = db.execute(
        select(Transaction)
        .where(Transaction.id == transaction_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    _check_switchable(transaction, settings)

    for prompt in _pending_stk_prompts(db, transaction.id):
        if current < _utc(prompt.sent_at) + PROMPT_LIFETIME:  # pragma: no cover - a race
            raise SwitchPromptOpenError(1)
        prompt.status = PaymentPromptStatus.FAILED.value
        prompt.failure_reason = PaymentFailureReason.TIMEOUT.value
        prompt.resolved_at = current

    from_rail = transaction.collection_rail
    transaction.payment_method = PAYMENT_METHOD_CARD
    # As for a card order created from a Trust Link: Pesapal collects, no custody escrow (#251).
    transaction.custody_provider = PROVIDER_SIMULATED
    set_collection(transaction, settings, RAIL_PESAPAL)
    record_money_audit_event(
        db,
        action=SWITCHED,
        actor_type=ACTOR_USER,
        actor_id=buyer.id,
        reason="The buyer chose to pay by card instead of M-Pesa.",
        transaction_id=transaction.id,
        rail_name=RAIL_PESAPAL,
        amount=transaction.amount,
        currency="KES",
        details={
            "from_method": "mpesa",
            "to_method": PAYMENT_METHOD_CARD,
            "from_rail": from_rail,
            "to_rail": RAIL_PESAPAL,
        },
        occurred_at=current,
    )
    audit_logger.info("payment_method_switched transaction_id=%s to=card", transaction.id)

    rail = card_rail or build_card_rail(settings, transaction=transaction)
    prompt = start_card_checkout(db, transaction, buyer, rail=rail, now=current)
    if prompt is None:
        # The order stays a card order; POST /payment/resend starts a new checkout.
        raise SwitchInitiationError(latest_payment_prompt(db, transaction.id))
    return prompt


def _check_switchable(transaction: Transaction, settings: Any) -> None:
    if transaction.status != TransactionStatus.AWAITING_PAYMENT:
        raise SwitchInvalidStateError("This order is not awaiting payment.")
    if transaction.payment_method == PAYMENT_METHOD_CARD:
        raise SwitchInvalidStateError("This order is already paid by card.")
    if (transaction.custody_provider or "").strip().lower() == PROVIDER_ECONFIRM:
        raise SwitchInvalidStateError("This order can only be paid with its M-Pesa prompt.")
    if not card_payments_available(settings):
        raise SwitchUnavailableError("Card payments are not available right now.")


def record_mpesa_after_card_switch(
    db: Session,
    transaction: Transaction,
    prompt: PaymentPrompt,
    *,
    receipt: str | None,
) -> str:
    """A confirmed M-Pesa payment on an order switched to card: audited once, never funds it.

    The anchored audit row is the alert: support refunds the M-Pesa payment by hand
    (docs/runbooks.md RB-13). The caller has already confirmed it with an STK query.
    """
    reference = prompt.checkout_request_id
    already = db.execute(
        select(MoneyAuditEvent.id).where(
            MoneyAuditEvent.transaction_id == transaction.id,
            MoneyAuditEvent.action == LATE_MPESA,
            # Any configured key: a row written before a key rotation still counts (#295).
            MoneyAuditEvent.provider_reference.in_(candidate_refs(reference)),
        )
    ).first()
    if already is not None:
        return "already_processed"
    record_money_audit_event(
        db,
        action=LATE_MPESA,
        actor_type=ACTOR_PROVIDER,
        reason=(
            "M-Pesa confirmed a payment after the buyer switched this order to card. The order "
            "is paid by card only: refund this M-Pesa payment to the buyer by hand."
        ),
        transaction_id=transaction.id,
        provider_reference=reference,
        rail_name=RAIL_DARAJA,
        amount=transaction.amount,
        currency="KES",
        details={"receipt_ref": provider_ref(receipt) if receipt else None},
    )
    db.commit()
    audit_logger.warning("mpesa_payment_after_card_switch transaction_id=%s", transaction.id)
    return "extra_payment"
