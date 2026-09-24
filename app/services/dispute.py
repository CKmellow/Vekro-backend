import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.transaction import Transaction


@dataclass(frozen=True)
class EscalationQueueItem:
    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    dispute_type: DisputeType
    dispute_status: DisputeStatus
    reason: str
    description: str
    opened_at: datetime
    escalated_at: datetime | None
    created_at: datetime


def list_escalated_disputes(db: Session) -> list[EscalationQueueItem]:
    rows = db.execute(
        select(Dispute, Transaction)
        .join(Transaction, Dispute.transaction_id == Transaction.id)
        .where(Dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW)
        .order_by(Dispute.escalated_at.desc().nullslast(), Dispute.created_at.desc())
    ).all()

    queue: list[EscalationQueueItem] = []
    for dispute, transaction in rows:
        if dispute.status != DisputeStatus.ESCALATED_ADMIN_REVIEW:
            continue
        queue.append(
            EscalationQueueItem(
                dispute_id=dispute.id,
                transaction_id=transaction.id,
                listing_id=transaction.listing_id,
                buyer_id=transaction.buyer_id,
                seller_id=transaction.seller_id,
                dispute_type=dispute.dispute_type,
                dispute_status=dispute.status,
                reason=dispute.reason,
                description=dispute.description,
                opened_at=dispute.opened_at,
                escalated_at=dispute.escalated_at,
                created_at=dispute.created_at,
            )
        )

    return queue
