"""Find and repair return cases left inconsistent by the timeout jobs before issue #221.

Two timeouts moved the transaction without moving its dispute:

- seller-received timeout: the transaction went to escalated_admin_review but the dispute stayed
  open, so no admin could resolve it and the money stayed held;
- buyer-sent-back timeout: the money was released but the dispute stayed open forever.

Any other open dispute behind a finished or escalated transaction is listed for a person to look
at and is never changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute, DisputeStatus
from app.models.transaction import Transaction, TransactionStatus
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event
from app.services.dispute_summary import OPEN_DISPUTE_STATUSES, latest_dispute

ESCALATE = "escalate"
CLOSE = "close"
REVIEW = "review"

# Transaction states in which an open dispute can no longer move on by itself.
_STUCK_TRANSACTION_STATUSES = frozenset(
    {
        TransactionStatus.ESCALATED_ADMIN_REVIEW,
        TransactionStatus.RELEASED,
        TransactionStatus.REFUNDED_BUYER,
        TransactionStatus.RESOLVED_RELEASE,
        TransactionStatus.RESOLVED_REFUND,
        TransactionStatus.RESOLVED_SPLIT,
    }
)


@dataclass(frozen=True)
class StrandedReturnCase:
    transaction_id: uuid.UUID
    reference: str
    dispute_id: uuid.UUID
    transaction_status: TransactionStatus
    dispute_status: DisputeStatus
    action: str  # ESCALATE, CLOSE or REVIEW (listed only)


def find_stranded_return_cases(db: Session) -> list[StrandedReturnCase]:
    transactions = (
        db.execute(
            select(Transaction)
            .where(Transaction.status.in_(_STUCK_TRANSACTION_STATUSES))
            .order_by(Transaction.created_at.asc())
        )
        .scalars()
        .all()
    )
    cases: list[StrandedReturnCase] = []
    for transaction in transactions:
        dispute = latest_dispute(db, transaction.id)
        if dispute is None or dispute.status not in OPEN_DISPUTE_STATUSES:
            continue
        if transaction.status == TransactionStatus.ESCALATED_ADMIN_REVIEW:
            action = ESCALATE
        elif transaction.status == TransactionStatus.RELEASED:
            action = CLOSE
        else:
            action = REVIEW
        cases.append(
            StrandedReturnCase(
                transaction_id=transaction.id,
                reference=transaction.reference,
                dispute_id=dispute.id,
                transaction_status=transaction.status,
                dispute_status=dispute.status,
                action=action,
            )
        )
    return cases


def repair_stranded_return_cases(
    db: Session, *, apply: bool, now: datetime | None = None
) -> list[StrandedReturnCase]:
    """List the stranded cases; with ``apply``, escalate or close them and commit once."""
    cases = find_stranded_return_cases(db)
    if not apply:
        return cases

    current = now or datetime.now(UTC)
    repaired = 0
    for case in cases:
        if case.action == REVIEW:
            continue
        dispute = db.get(Dispute, case.dispute_id)
        if dispute is None:  # pragma: no cover - found above
            continue
        if case.action == ESCALATE:
            dispute.status = DisputeStatus.ESCALATED_ADMIN_REVIEW
            dispute.escalated_at = dispute.escalated_at or current
        else:
            dispute.status = DisputeStatus.CANCELED
            dispute.resolved_at = dispute.resolved_at or current
        record_money_audit_event(
            db,
            action="return_case_repaired",
            actor_type=ACTOR_SYSTEM,
            reason=f"repair-return-cases: {case.action}",
            transaction_id=case.transaction_id,
            details={
                "dispute_id": str(case.dispute_id),
                "from": case.dispute_status.value,
                "to": dispute.status.value,
            },
            occurred_at=current,
        )
        repaired += 1
    if repaired:
        db.commit()
    return cases
