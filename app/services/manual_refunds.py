"""Manual refunds of card-funded orders (M22, #253).

Card money sits in the merchant's Pesapal account and cannot be paid back over M-Pesa. Pesapal
refunds need the merchant's approval and Pesapal's own processing (docs/pesapal-observed.md
section 7), so a card order's refund is queued to ``manual_card_refund``: the payout executor never
sends it. Support refunds the payment in Pesapal, then completes it here. Completion settles the
attempt through the executor's own path (funded-balance guard, ledger movement, escrow totals,
payout status) and, with ANCHOR_GATE_PAYOUTS, only once the refund's approval is anchored.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.models.escrow import Escrow
from app.models.payment_prompt import PaymentPrompt, PaymentPromptKind
from app.models.payout_attempt import AttemptOutcome, PayoutAttempt
from app.models.transaction import Transaction
from app.services.audit import ACTOR_ADMIN, record_money_audit_event
from app.services.escrow_service import (
    MANUAL_CARD_REFUND_RAIL,
    EscrowService,
    _audit_payout_attempt,
    settle_succeeded_payout,
)


class ManualRefundNotFoundError(LookupError):
    pass


class ManualRefundRefusedError(RuntimeError):
    """The refund cannot be completed yet (unanchored approval, or not enough held money)."""


@dataclass(frozen=True)
class ManualRefund:
    attempt_id: uuid.UUID
    transaction_id: uuid.UUID
    reference: str
    amount: Decimal
    currency: str
    queued_at: datetime
    pesapal_order_tracking_id: str | None
    status: str  # pending or completed


def _tracking_id(db: Session, transaction_id: uuid.UUID) -> str | None:
    """The Pesapal order that took the money: what support looks up in Pesapal's dashboard."""
    return db.execute(
        select(PaymentPrompt.checkout_request_id)
        .where(
            PaymentPrompt.transaction_id == transaction_id,
            PaymentPrompt.kind == PaymentPromptKind.REDIRECT.value,
            PaymentPrompt.status == "succeeded",
        )
        .order_by(PaymentPrompt.sent_at.desc())
        .limit(1)
    ).scalar()


def _view(db: Session, attempt: PayoutAttempt, transaction: Transaction) -> ManualRefund:
    return ManualRefund(
        attempt_id=attempt.id,
        transaction_id=transaction.id,
        reference=transaction.reference,
        amount=Decimal(attempt.amount),
        currency=attempt.currency,
        queued_at=attempt.created_at,
        pesapal_order_tracking_id=_tracking_id(db, transaction.id),
        status="completed" if attempt.outcome == AttemptOutcome.SUCCEEDED else "pending",
    )


def list_manual_refunds(db: Session, *, include_completed: bool = False) -> list[ManualRefund]:
    statement = (
        select(PayoutAttempt, Transaction)
        .join(Escrow, Escrow.id == PayoutAttempt.escrow_id)
        .join(Transaction, Transaction.id == Escrow.transaction_id)
        .where(PayoutAttempt.rail_name == MANUAL_CARD_REFUND_RAIL)
        .order_by(PayoutAttempt.created_at)
    )
    if not include_completed:
        statement = statement.where(PayoutAttempt.outcome == AttemptOutcome.UNKNOWN)
    return [_view(db, attempt, transaction) for attempt, transaction in db.execute(statement)]


def complete_manual_refund(
    db: Session,
    *,
    attempt_id: uuid.UUID,
    admin_id: uuid.UUID,
    note: str,
    provider_refund_reference: str | None = None,
    anchor_root_of: Callable[[int], bytes] | None = None,
    now: datetime | None = None,
) -> ManualRefund:
    """Record that support refunded the card payment in Pesapal. Idempotent."""
    attempt = db.execute(
        select(PayoutAttempt).where(PayoutAttempt.id == attempt_id).with_for_update()
    ).scalar()
    if attempt is None or attempt.rail_name != MANUAL_CARD_REFUND_RAIL:
        raise ManualRefundNotFoundError("Manual refund not found.")
    escrow = db.get(Escrow, attempt.escrow_id)
    transaction = db.get(Transaction, escrow.transaction_id) if escrow else None
    if escrow is None or transaction is None:  # pragma: no cover - foreign keys prevent it
        raise ManualRefundNotFoundError("Manual refund not found.")
    if attempt.outcome == AttemptOutcome.SUCCEEDED:
        return _view(db, attempt, transaction)

    settings = get_settings()
    if settings.anchor_gate_payouts:
        from app.services.anchoring.chain import build_registry_client
        from app.services.anchoring.payout_gate import check_payout_gate

        decision = check_payout_gate(
            db,
            attempt,
            transaction_id=str(transaction.id),
            destination_phone=str(attempt.request_snapshot.get("destination_phone") or ""),
            root_of=anchor_root_of or build_registry_client(settings, with_key=False).root_of,
        )
        if not decision.allowed:
            raise ManualRefundRefusedError(
                f"The refund's approval is not anchored yet ({decision.code})."
            )

    current = now or datetime.now(UTC)
    attempt.outcome = AttemptOutcome.SUCCEEDED
    attempt.provider_reference = (provider_refund_reference or "").strip() or None
    attempt.response_snapshot = {
        **(attempt.response_snapshot or {}),
        "completed_manually": True,
        "note": note,
    }
    attempt.failure_code = None
    attempt.failure_reason = None
    attempt.attempted_at = current
    if not settle_succeeded_payout(db, attempt, escrow, payout_kind="refund"):
        db.rollback()
        raise ManualRefundRefusedError("The escrow's held balance does not cover this refund.")

    _audit_payout_attempt(db, attempt=attempt, transaction=transaction, kind="refund")
    record_money_audit_event(
        db,
        action="manual_refund_completed",
        actor_type=ACTOR_ADMIN,
        actor_id=admin_id,
        reason=note,
        transaction_id=transaction.id,
        escrow_id=escrow.id,
        attempt_id=attempt.id,
        provider_reference=attempt.provider_reference,
        rail_name=MANUAL_CARD_REFUND_RAIL,
        amount=attempt.amount,
        currency=attempt.currency,
        occurred_at=current,
    )
    EscrowService(db).refresh_transaction_payout_status(transaction)
    db.add(transaction)
    db.commit()
    return _view(db, attempt, transaction)
