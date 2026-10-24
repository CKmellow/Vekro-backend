"""Where the seller's money is for one order, in words the seller's payout card can show."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection_attempt import AttemptOutcome
from app.models.escrow import Escrow
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus


class SellerPayoutStatus(StrEnum):
    NOT_PAID = "not_paid"
    HELD = "held"
    IN_PROGRESS = "in_progress"
    PAID_OUT = "paid_out"
    DELAYED = "delayed"
    REFUNDED = "refunded"


UNPAID = {TransactionStatus.INITIATED, TransactionStatus.AWAITING_PAYMENT}
REFUNDED = {
    TransactionStatus.REFUNDED_BUYER,
    TransactionStatus.RESOLVED_REFUND,
    TransactionStatus.RETURNED_TO_SELLER,
}
RELEASED = {
    TransactionStatus.RELEASED,
    TransactionStatus.RESOLVED_RELEASE,
    TransactionStatus.RESOLVED_SPLIT,
}


@dataclass(frozen=True)
class SellerPayout:
    status: SellerPayoutStatus
    amount: Decimal | None
    reference: str | None
    paid_at: datetime | None


def _release_attempts(db: Session, transaction: Transaction) -> list[PayoutAttempt]:
    """Newest first. Every release purpose (tx-release, hold-release, admin-split-release...) has
    "release:" in it; refunds never do."""
    return [
        attempt
        for attempt in db.execute(
            select(PayoutAttempt)
            .join(Escrow, Escrow.id == PayoutAttempt.escrow_id)
            .where(Escrow.transaction_id == transaction.id)
            .order_by(PayoutAttempt.created_at.desc())
        ).scalars()
        if "release:" in attempt.purpose
    ]


def seller_payout(db: Session, transaction: Transaction) -> SellerPayout:
    status = transaction.status
    if status in UNPAID:
        return SellerPayout(SellerPayoutStatus.NOT_PAID, None, None, None)
    if status in REFUNDED:
        return SellerPayout(SellerPayoutStatus.REFUNDED, None, None, None)
    if status not in RELEASED:
        return SellerPayout(SellerPayoutStatus.HELD, None, None, None)

    attempts = _release_attempts(db, transaction)
    latest = attempts[0] if attempts else None
    amount = latest.amount if latest else transaction.amount
    if latest is not None and latest.outcome == AttemptOutcome.SUCCEEDED:
        return SellerPayout(
            SellerPayoutStatus.PAID_OUT, amount, latest.provider_reference, latest.updated_at
        )
    if transaction.payout_status == TransactionPayoutStatus.FAILED_DEFINITE or (
        latest is not None and latest.outcome == AttemptOutcome.FAILED_DEFINITE
    ):
        return SellerPayout(SellerPayoutStatus.DELAYED, amount, None, None)
    return SellerPayout(SellerPayoutStatus.IN_PROGRESS, amount, None, None)
