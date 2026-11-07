"""Find and repair return cases left inconsistent by the timeout jobs before issue #221.

Two timeouts moved the transaction without moving its dispute:

- seller-received timeout: the transaction went to escalated_admin_review but the dispute stayed
  open, so no admin could resolve it and the money stayed held;
- buyer-sent-back timeout: the money was released but the dispute stayed open forever.

A functional issue reported as "other" (#227) escalated the dispute but left the transaction in
disputed_functional, where no admin could resolve it. Those transactions are escalated too. If
the buyer-sent-back timeout already released one, it is listed for review.

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
from app.services.transitions import transition_transaction

ESCALATE = "escalate"
CLOSE = "close"
REVIEW = "review"
ESCALATE_TRANSACTION = "escalate_transaction"

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
        if dispute is None:
            continue
        if dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW:
            # #227: an "other" case the timeout released while support still held it.
            if transaction.status == TransactionStatus.RELEASED:
                cases.append(_case(transaction, dispute, REVIEW))
            continue
        if dispute.status not in OPEN_DISPUTE_STATUSES:
            continue
        if transaction.status == TransactionStatus.ESCALATED_ADMIN_REVIEW:
            action = ESCALATE
        elif transaction.status == TransactionStatus.RELEASED:
            action = CLOSE
        else:
            action = REVIEW
        cases.append(_case(transaction, dispute, action))
    cases.extend(_unescalated_other_cases(db))
    return cases


def _unescalated_other_cases(db: Session) -> list[StrandedReturnCase]:
    rows = (
        db.execute(
            select(Transaction, Dispute)
            .join(Dispute, Dispute.transaction_id == Transaction.id)
            .where(
                Transaction.status == TransactionStatus.DISPUTED_FUNCTIONAL,
                Dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW,
            )
            .order_by(Transaction.created_at.asc())
        )
        .tuples()
        .all()
    )
    return [_case(transaction, dispute, ESCALATE_TRANSACTION) for transaction, dispute in rows]


def _case(transaction: Transaction, dispute: Dispute, action: str) -> StrandedReturnCase:
    return StrandedReturnCase(
        transaction_id=transaction.id,
        reference=transaction.reference,
        dispute_id=dispute.id,
        transaction_status=transaction.status,
        dispute_status=dispute.status,
        action=action,
    )


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
        if case.action == ESCALATE_TRANSACTION:
            transaction = db.get(Transaction, case.transaction_id)
            if transaction is None:  # pragma: no cover - found above
                continue
            transition_transaction(
                db,
                transaction,
                TransactionStatus.ESCALATED_ADMIN_REVIEW,
                actor_type=ACTOR_SYSTEM,
                reason="repair_other_category",
                at=current,
            )
            record_money_audit_event(
                db,
                action="return_case_repaired",
                actor_type=ACTOR_SYSTEM,
                reason=f"repair-return-cases: {case.action}",
                transaction_id=case.transaction_id,
                details={
                    "dispute_id": str(case.dispute_id),
                    "from": case.transaction_status.value,
                    "to": TransactionStatus.ESCALATED_ADMIN_REVIEW.value,
                },
                occurred_at=current,
            )
            repaired += 1
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
