"""Payment prompts: what the buyer was asked to do to pay, and how it ended."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.payment_prompt import (
    PaymentFailureReason,
    PaymentPrompt,
    PaymentPromptKind,
    PaymentPromptStatus,
)
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.services.payment import (
    PaymentGateway,
    PaymentRequest,
    PaymentResponse,
    get_payment_gateway,
)

audit_logger = logging.getLogger("app.transactions.audit")

# An STK prompt stays open on the phone for about a minute; clients poll for 90 seconds.
PROMPT_LIFETIME = timedelta(seconds=90)
RESEND_COOLDOWN = timedelta(seconds=30)

# M-Pesa STK result codes. Anything else is a provider error.
FAILURE_REASON_BY_RESULT_CODE: dict[int, PaymentFailureReason] = {
    1: PaymentFailureReason.INSUFFICIENT_FUNDS,
    1032: PaymentFailureReason.CANCELLED,
    1037: PaymentFailureReason.TIMEOUT,
    2001: PaymentFailureReason.WRONG_PIN,
}

EXPIRED = "expired"


class PaymentResendNotFoundError(Exception):
    pass


class PaymentResendInvalidStateError(Exception):
    pass


class PaymentResendTooSoonError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Wait before sending another payment prompt.")
        self.retry_after_seconds = retry_after_seconds


class PaymentResendInitiationError(Exception):
    def __init__(self, prompt: PaymentPrompt) -> None:
        super().__init__("Failed to initiate payment transport.")
        self.prompt = prompt


@dataclass(frozen=True)
class PaymentPromptState:
    """A prompt as clients see it: an open prompt past its expiry reads as expired."""

    kind: PaymentPromptKind
    status: str
    failure_reason: PaymentFailureReason | None
    redirect_url: str | None
    sent_at: datetime
    expires_at: datetime
    resend_available_at: datetime


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def failure_reason_for(result_code: int) -> PaymentFailureReason:
    return FAILURE_REASON_BY_RESULT_CODE.get(result_code, PaymentFailureReason.PROVIDER_ERROR)


def record_payment_prompt(
    db: Session,
    transaction: Transaction,
    *,
    payment_response: PaymentResponse | None,
    now: datetime | None = None,
) -> PaymentPrompt:
    """Stores the prompt just sent, or a failed_to_initiate one when sending failed."""
    sent_at = now or datetime.now(UTC)
    prompt = PaymentPrompt(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        kind=PaymentPromptKind.STK_PROMPT.value,
        status=(
            PaymentPromptStatus.PENDING.value
            if payment_response is not None
            else PaymentPromptStatus.FAILED_TO_INITIATE.value
        ),
        checkout_request_id=payment_response.checkout_request_id if payment_response else None,
        sent_at=sent_at,
        expires_at=sent_at + PROMPT_LIFETIME,
        resolved_at=None if payment_response is not None else sent_at,
    )
    db.add(prompt)
    db.commit()
    return prompt


def latest_payment_prompt(db: Session, transaction_id: uuid.UUID) -> PaymentPrompt | None:
    return (
        db.execute(
            select(PaymentPrompt)
            .where(PaymentPrompt.transaction_id == transaction_id)
            .order_by(PaymentPrompt.sent_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def prompt_state(prompt: PaymentPrompt, *, now: datetime | None = None) -> PaymentPromptState:
    current = now or datetime.now(UTC)
    sent_at = _utc(prompt.sent_at)
    expires_at = _utc(prompt.expires_at)
    status = prompt.status
    if status == PaymentPromptStatus.PENDING.value and current >= expires_at:
        status = EXPIRED
    reason = PaymentFailureReason(prompt.failure_reason) if prompt.failure_reason else None
    return PaymentPromptState(
        kind=PaymentPromptKind(prompt.kind),
        status=status,
        failure_reason=reason,
        redirect_url=prompt.redirect_url if prompt.kind == PaymentPromptKind.REDIRECT else None,
        sent_at=sent_at,
        expires_at=expires_at,
        resend_available_at=sent_at + RESEND_COOLDOWN,
    )


def resolve_open_prompt(
    db: Session,
    transaction_id: uuid.UUID,
    *,
    result_code: int,
    now: datetime | None = None,
) -> PaymentPrompt | None:
    """Records a callback's outcome on the open prompt. The caller commits."""
    prompt = latest_payment_prompt(db, transaction_id)
    if prompt is None or prompt.status != PaymentPromptStatus.PENDING.value:
        return None
    prompt.resolved_at = now or datetime.now(UTC)
    if result_code == 0:
        prompt.status = PaymentPromptStatus.SUCCEEDED.value
    else:
        prompt.status = PaymentPromptStatus.FAILED.value
        prompt.failure_reason = failure_reason_for(result_code).value
    return prompt


def resend_payment_prompt(
    db: Session,
    *,
    transaction_id: uuid.UUID,
    buyer: User,
    payment_gateway: PaymentGateway | None = None,
    now: datetime | None = None,
) -> PaymentPrompt:
    """Sends a new STK prompt for an unpaid transaction, at most once per cooldown."""
    current = now or datetime.now(UTC)
    transaction = db.get(Transaction, transaction_id)
    if transaction is None or transaction.buyer_id != buyer.id:
        raise PaymentResendNotFoundError("Transaction not found.")
    if transaction.status != TransactionStatus.AWAITING_PAYMENT:
        raise PaymentResendInvalidStateError("Transaction is not awaiting payment.")

    previous = latest_payment_prompt(db, transaction.id)
    if previous is not None:
        available_at = _utc(previous.sent_at) + RESEND_COOLDOWN
        if current < available_at:
            wait = int((available_at - current).total_seconds()) + 1
            raise PaymentResendTooSoonError(retry_after_seconds=wait)
        if previous.status == PaymentPromptStatus.PENDING.value:
            # Superseded by the new prompt; a late callback for it changes nothing.
            previous.status = PaymentPromptStatus.FAILED.value
            previous.failure_reason = PaymentFailureReason.TIMEOUT.value
            previous.resolved_at = current

    listing = db.get(Listing, transaction.listing_id)
    gateway = payment_gateway or get_payment_gateway()
    try:
        response = gateway.initiate_stk_push(
            PaymentRequest(
                transaction_id=str(transaction.id),
                amount=f"{transaction.amount:.2f}",
                phone_number=buyer.mpesa_phone or buyer.phone,
                account_reference=str(transaction.id),
                transaction_desc=f"Escrow payment for listing {listing.id if listing else ''}",
            )
        )
    except Exception as exc:
        prompt = record_payment_prompt(db, transaction, payment_response=None, now=current)
        raise PaymentResendInitiationError(prompt) from exc

    audit_logger.info("payment_prompt_resent transaction_id=%s", transaction.id)
    return record_payment_prompt(db, transaction, payment_response=response, now=current)
