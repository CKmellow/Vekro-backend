import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.notification import Notification
from app.models.transaction import Transaction, TransactionStatus


class DisputeCaseNotFoundError(Exception):
    pass


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


@dataclass(frozen=True)
class DisputeTimelineEvent:
    source: str
    event_type: str
    at: datetime
    title: str | None
    message: str | None
    payload: dict


@dataclass(frozen=True)
class DisputeCaseTimeline:
    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    opened_by_user_id: uuid.UUID
    transaction_status: TransactionStatus
    amount: Decimal
    dispute_type: DisputeType
    dispute_status: DisputeStatus
    reason: str
    description: str
    opened_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    timeline_events: list[DisputeTimelineEvent]


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


def get_dispute_case_timeline(db: Session, dispute_id: uuid.UUID) -> DisputeCaseTimeline:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise DisputeCaseNotFoundError("Dispute not found.")

    transaction = db.get(Transaction, dispute.transaction_id)
    if transaction is None:
        raise DisputeCaseNotFoundError("Transaction for dispute not found.")

    notifications = list(
        db.execute(
            select(Notification)
            .where(Notification.transaction_id == transaction.id)
            .order_by(Notification.created_at.asc())
        )
        .scalars()
        .all()
    )

    timeline_events = [
        DisputeTimelineEvent(
            source="dispute",
            event_type="dispute_opened",
            at=dispute.opened_at,
            title="Dispute opened",
            message="Dispute case was opened.",
            payload={"dispute_status": dispute.status.value},
        )
    ]

    if dispute.escalated_at is not None:
        timeline_events.append(
            DisputeTimelineEvent(
                source="dispute",
                event_type="dispute_escalated",
                at=dispute.escalated_at,
                title="Dispute escalated",
                message="Dispute was escalated for admin review.",
                payload={"dispute_status": dispute.status.value},
            )
        )

    if dispute.resolved_at is not None:
        timeline_events.append(
            DisputeTimelineEvent(
                source="dispute",
                event_type="dispute_resolved",
                at=dispute.resolved_at,
                title="Dispute resolved",
                message="Dispute reached a resolved state.",
                payload={"dispute_status": dispute.status.value},
            )
        )

    for notification in notifications:
        timeline_events.append(
            DisputeTimelineEvent(
                source="notification",
                event_type=notification.event_type.value,
                at=notification.created_at,
                title=notification.title,
                message=notification.message,
                payload=notification.payload,
            )
        )

    timeline_events.sort(key=lambda item: item.at)

    return DisputeCaseTimeline(
        dispute_id=dispute.id,
        transaction_id=transaction.id,
        listing_id=transaction.listing_id,
        buyer_id=transaction.buyer_id,
        seller_id=transaction.seller_id,
        opened_by_user_id=dispute.opened_by_user_id,
        transaction_status=transaction.status,
        amount=transaction.amount,
        dispute_type=dispute.dispute_type,
        dispute_status=dispute.status,
        reason=dispute.reason,
        description=dispute.description,
        opened_at=dispute.opened_at,
        escalated_at=dispute.escalated_at,
        resolved_at=dispute.resolved_at,
        timeline_events=timeline_events,
    )
