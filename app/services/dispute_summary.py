"""The dispute behind an order, as its buyer, its seller or an admin may see it."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import AdminDecision, Dispute, DisputeStatus, SellerResolutionAction

# A return case still in progress: neither resolved by a party or an admin, nor closed.
OPEN_DISPUTE_STATUSES = frozenset(
    {
        DisputeStatus.OPEN,
        DisputeStatus.BUYER_SENT_BACK,
        DisputeStatus.RETURN_RECEIVED,
        DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
    }
)


@dataclass(frozen=True)
class DisputeSummary:
    id: uuid.UUID
    status: DisputeStatus
    category: str
    description: str
    opened_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    seller_action: SellerResolutionAction | None
    seller_notes: str | None
    buyer_notes: str | None
    reconfirm_rejections: int
    buyer_confirmed_resolved: bool
    seller_confirmed_resolved: bool
    admin_decision: AdminDecision | None


def latest_dispute(db: Session, transaction_id: uuid.UUID) -> Dispute | None:
    return (
        db.execute(
            select(Dispute)
            .where(Dispute.transaction_id == transaction_id)
            .order_by(Dispute.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def summarize(dispute: Dispute) -> DisputeSummary:
    """Only named facts leave the server: the raw evidence blob never does."""
    evidence = dispute.evidence or {}
    return DisputeSummary(
        id=dispute.id,
        status=dispute.status,
        category=dispute.reason,
        description=dispute.description,
        opened_at=dispute.opened_at,
        escalated_at=dispute.escalated_at,
        resolved_at=dispute.resolved_at,
        seller_action=dispute.seller_resolution_action,
        seller_notes=dispute.seller_resolution_notes,
        buyer_notes=evidence.get("buyer_reconfirm_notes") or None,
        reconfirm_rejections=int(evidence.get("buyer_reconfirm_rejection_count", 0) or 0),
        buyer_confirmed_resolved=bool(evidence.get("buyer_confirmed_resolved", False)),
        seller_confirmed_resolved=bool(evidence.get("seller_confirmed_resolved", False)),
        admin_decision=dispute.admin_decision,
    )


def dispute_summary(db: Session, transaction_id: uuid.UUID) -> DisputeSummary | None:
    dispute = latest_dispute(db, transaction_id)
    return summarize(dispute) if dispute is not None else None


def latest_categories(db: Session, transaction_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The newest dispute's category per transaction, in one query."""
    if not transaction_ids:
        return {}
    categories: dict[uuid.UUID, str] = {}
    for transaction_id, reason in db.execute(
        select(Dispute.transaction_id, Dispute.reason)
        .where(Dispute.transaction_id.in_(transaction_ids))
        .order_by(Dispute.created_at.asc())
    ).all():
        categories[transaction_id] = reason  # newest written last
    return categories
