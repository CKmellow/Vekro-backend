"""Card payments through Pesapal's hosted checkout (M22, #251; docs/pesapal-observed.md).

The buyer is sent to Pesapal's page (``PaymentInstruction`` REDIRECT) and comes back to the
frontend return page. Pesapal's IPN and the buyer's return are only triggers: Vekro records the
funding after its own GetTransactionStatus reports COMPLETED for the order's exact amount,
currency and merchant reference, the same rule as the Daraja callback (#224). Anything else
pending or unknown changes nothing; a definite failure closes the prompt; a mismatch is refused
and audited. Card money is collected into the merchant's Pesapal account: collection, not
third-party escrow custody.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_prompt import (
    PaymentFailureReason,
    PaymentPrompt,
    PaymentPromptKind,
    PaymentPromptStatus,
)
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.schemas.transaction import PaymentCallbackRequest
from app.services.audit import ACTOR_PROVIDER, record_money_audit_event
from app.services.custody.dto import CollectionResult, FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalAuthError, PesapalTokenManager
from app.services.custody.pesapal_collection import PesapalCollectionRail

_logger = logging.getLogger("app.payments.card")

RAIL = "pesapal"
PAYMENT_METHOD_CARD = "card"
# Long enough for the card form and a 3D Secure challenge.
CHECKOUT_LIFETIME = timedelta(minutes=30)
RECONCILE_WINDOW = timedelta(hours=24)


def card_payments_available(settings: Any) -> bool:
    return bool(
        settings.pesapal_enabled
        and settings.pesapal_base_url.strip()
        and settings.pesapal_consumer_key.strip()
        and settings.pesapal_consumer_secret.strip()
        and settings.pesapal_ipn_id.strip()
        and settings.frontend_url.strip()
    )


def return_url(settings: Any, transaction: Transaction) -> str:
    """Where Pesapal sends the buyer back: the frontend return page, never the backend. ``order``
    lets the page read the order; the result always comes from the API, never the URL (#255)."""
    base = settings.frontend_url.rstrip("/")
    return f"{base}/pay/return?order={transaction.id}&ref={quote(transaction.reference)}"


def build_rail(settings: Any, *, transaction: Any = None) -> PesapalCollectionRail:
    return PesapalCollectionRail(
        base_url=settings.pesapal_base_url,
        callback_url=return_url(settings, transaction) if transaction is not None else "",
        ipn_id=settings.pesapal_ipn_id,
        token_manager=PesapalTokenManager(
            base_url=settings.pesapal_base_url,
            consumer_key=settings.pesapal_consumer_key,
            consumer_secret=settings.pesapal_consumer_secret,
        ),
    )


def start_card_checkout(
    db: Session,
    transaction: Transaction,
    buyer: User,
    *,
    rail: PesapalCollectionRail,
    now: datetime | None = None,
) -> PaymentPrompt | None:
    """Submit the order to Pesapal and record a REDIRECT prompt; None when Pesapal refused it.

    Pesapal needs the buyer's phone or e-mail for the billing details; the phone is sent.
    """
    sent_at = now or datetime.now(UTC)
    try:
        result = rail.request_funding(
            FundingRequest(
                escrow_reference=transaction.reference,
                amount=Decimal(transaction.amount),
                phone_number=buyer.mpesa_phone or buyer.phone,
                account_reference=transaction.reference,
                payer_name=buyer.name or "",
            )
        )
    except (PesapalAuthError, RuntimeError, ValueError) as exc:
        _logger.warning(
            "card_checkout_unavailable transaction_id=%s error=%s", transaction.id, type(exc)
        )
        result = None
    redirect = (result.metadata or {}).get("redirect_url") if result else None
    if result is None or result.outcome != CollectionOutcome.SUCCEEDED or not redirect:
        _record_prompt(db, transaction, sent_at=sent_at, tracking_id=None, redirect_url=None)
        return None
    return _record_prompt(
        db,
        transaction,
        sent_at=sent_at,
        tracking_id=result.provider_reference,
        redirect_url=str(redirect),
    )


def _record_prompt(
    db: Session,
    transaction: Transaction,
    *,
    sent_at: datetime,
    tracking_id: str | None,
    redirect_url: str | None,
) -> PaymentPrompt:
    started = tracking_id is not None
    prompt = PaymentPrompt(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        kind=PaymentPromptKind.REDIRECT.value,
        status=(
            PaymentPromptStatus.PENDING.value
            if started
            else PaymentPromptStatus.FAILED_TO_INITIATE.value
        ),
        checkout_request_id=tracking_id,
        redirect_url=redirect_url,
        sent_at=sent_at,
        expires_at=sent_at + CHECKOUT_LIFETIME,
        resolved_at=None if started else sent_at,
    )
    db.add(prompt)
    db.commit()
    return prompt


def confirm_card_payment(
    db: Session,
    transaction: Transaction,
    prompt: PaymentPrompt,
    *,
    rail: PesapalCollectionRail,
) -> str:
    """Ask Pesapal for the order's status; fund only on COMPLETED with the exact amount.

    Returns confirmed, failed, pending, mismatch, already_processed or unconfirmed.
    """
    if transaction.status != TransactionStatus.AWAITING_PAYMENT:
        return "already_processed"
    if prompt.status != PaymentPromptStatus.PENDING.value or not prompt.checkout_request_id:
        return "already_processed"
    try:
        result = rail.get_funding_status(prompt.checkout_request_id)
    except (PesapalAuthError, RuntimeError, ValueError) as exc:
        _logger.warning("card_status_unavailable transaction_id=%s error=%s", transaction.id, exc)
        return "unconfirmed"

    if result.outcome == CollectionOutcome.UNKNOWN:
        return "pending"
    if result.outcome == CollectionOutcome.FAILED_DEFINITE:
        _close_prompt(prompt, failed=True)
        record_money_audit_event(
            db,
            action="card_payment_failed",
            actor_type=ACTOR_PROVIDER,
            reason=f"Pesapal reported the card payment as {result.raw_status}.",
            transaction_id=transaction.id,
            provider_reference=prompt.checkout_request_id,
            rail_name=RAIL,
            currency="KES",
        )
        db.commit()
        return "failed"

    mismatch = _mismatch(transaction, result)
    if mismatch:
        record_money_audit_event(
            db,
            action="card_payment_mismatch",
            actor_type=ACTOR_PROVIDER,
            reason=f"Pesapal reported a completed payment that does not match: {mismatch}.",
            transaction_id=transaction.id,
            provider_reference=prompt.checkout_request_id,
            rail_name=RAIL,
            amount=_decimal(result.metadata.get("amount")),
            currency=str(result.metadata.get("currency") or "") or None,
            details={"expected_amount": str(transaction.amount), "expected_currency": "KES"},
        )
        # Closed so a replayed IPN or the next check cannot audit it twice; the order stays
        # unpaid for support to resolve with Pesapal.
        prompt.status = PaymentPromptStatus.FAILED.value
        prompt.failure_reason = PaymentFailureReason.PROVIDER_ERROR.value
        prompt.resolved_at = datetime.now(UTC)
        db.commit()
        _logger.warning(
            "card_payment_mismatch transaction_id=%s problem=%s", transaction.id, mismatch
        )
        return "mismatch"

    from app.services.transaction import confirm_payment_callback

    confirm_payment_callback(
        db,
        PaymentCallbackRequest(
            transaction_id=transaction.id,
            result_code=0,
            result_desc="Card payment completed (Pesapal status query).",
            checkout_request_id=prompt.checkout_request_id,
            provider_reference=result.metadata.get("confirmation_code")
            or prompt.checkout_request_id,
        ),
        confirmed_by=RAIL,
    )
    return "confirmed"


def _mismatch(transaction: Transaction, result: CollectionResult) -> str | None:
    metadata = result.metadata or {}
    paid = _decimal(metadata.get("amount"))
    if paid is None or paid != Decimal(transaction.amount):
        return f"amount {metadata.get('amount')} instead of {transaction.amount}"
    if str(metadata.get("currency") or "").upper() != "KES":
        return f"currency {metadata.get('currency')} instead of KES"
    reference = metadata.get("merchant_reference")
    if reference and reference != transaction.reference:
        return "a different merchant reference"
    return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _close_prompt(prompt: PaymentPrompt, *, failed: bool) -> None:
    if failed:
        prompt.status = PaymentPromptStatus.FAILED.value
        prompt.failure_reason = PaymentFailureReason.DECLINED.value
    prompt.resolved_at = datetime.now(UTC)


def card_prompt_for_tracking_id(
    db: Session, tracking_id: str
) -> tuple[Transaction, PaymentPrompt] | None:
    row = db.execute(
        select(Transaction, PaymentPrompt)
        .join(PaymentPrompt, PaymentPrompt.transaction_id == Transaction.id)
        .where(
            PaymentPrompt.kind == PaymentPromptKind.REDIRECT.value,
            PaymentPrompt.checkout_request_id == tracking_id,
        )
    ).first()
    return (row[0], row[1]) if row else None


def handle_ipn(
    db: Session, params: dict[str, Any], *, rail: PesapalCollectionRail
) -> dict[str, Any]:
    """Pesapal's IPN: a trigger only. Answers the JSON Pesapal documents (status 200 or 500)."""
    tracking_id = str(params.get("OrderTrackingId") or "").strip()
    merchant_reference = str(params.get("OrderMerchantReference") or "").strip()
    answer = {
        "orderNotificationType": str(params.get("OrderNotificationType") or "IPNCHANGE"),
        "orderTrackingId": tracking_id,
        "orderMerchantReference": merchant_reference,
        "status": 500,
    }
    match = card_prompt_for_tracking_id(db, tracking_id) if tracking_id else None
    if match is None or match[0].reference != merchant_reference:
        _logger.warning("card_ipn_unknown_order merchant_reference=%s", merchant_reference[:20])
        return answer  # refused: not an order Vekro sent to Pesapal
    transaction, prompt = match
    outcome = confirm_card_payment(db, transaction, prompt, rail=rail)
    _logger.info("card_ipn_processed transaction_id=%s outcome=%s", transaction.id, outcome)
    return {**answer, "status": 500 if outcome == "unconfirmed" else 200}


def refresh_card_payment(
    db: Session, transaction: Transaction, *, rail: PesapalCollectionRail
) -> str:
    """The buyer is back on the return page, maybe before the IPN: check now."""
    prompt = db.execute(
        select(PaymentPrompt)
        .where(
            PaymentPrompt.transaction_id == transaction.id,
            PaymentPrompt.kind == PaymentPromptKind.REDIRECT.value,
        )
        .order_by(PaymentPrompt.sent_at.desc())
        .limit(1)
    ).scalar()
    if prompt is None:
        return "no_card_checkout"
    return confirm_card_payment(db, transaction, prompt, rail=rail)


def confirm_pending_card_payments(
    db: Session, *, rail: PesapalCollectionRail, now: datetime | None = None
) -> dict[uuid.UUID, str]:
    """Settle open card checkouts from the last day (scheduler, or `confirm-card-payments`)."""
    since = (now or datetime.now(UTC)) - RECONCILE_WINDOW
    rows = db.execute(
        select(Transaction, PaymentPrompt)
        .join(PaymentPrompt, PaymentPrompt.transaction_id == Transaction.id)
        .where(
            Transaction.status == TransactionStatus.AWAITING_PAYMENT,
            PaymentPrompt.kind == PaymentPromptKind.REDIRECT.value,
            PaymentPrompt.status == PaymentPromptStatus.PENDING.value,
            PaymentPrompt.sent_at >= since,
        )
        .order_by(PaymentPrompt.sent_at)
    ).all()
    return {
        transaction.id: confirm_card_payment(db, transaction, prompt, rail=rail)
        for transaction, prompt in rows
    }
