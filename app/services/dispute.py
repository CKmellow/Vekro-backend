import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import AdminDecision, Dispute, DisputeStatus, DisputeType
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.services.audit import ACTOR_ADMIN, record_money_audit_event
from app.services.escrow_service import EscrowService, SplitPayoutUnsupportedError


class DisputeCaseNotFoundError(Exception):
    pass


class DisputeCaseInvalidStateError(Exception):
    pass


class DisputeDecisionReasonRequiredError(Exception):
    pass


class DisputeSplitPayoutUnsupportedError(Exception):
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


@dataclass(frozen=True)
class AdminForceResolveResult:
    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    decision: AdminDecision
    reason: str
    dispute_status: DisputeStatus
    transaction_status: TransactionStatus
    resolved_at: datetime
    released_at: datetime | None
    refunded_at: datetime | None


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


def force_resolve_dispute_case(
    db: Session,
    dispute_id: uuid.UUID,
    *,
    decision: AdminDecision,
    reason: str,
    actor_id: uuid.UUID | None = None,
) -> AdminForceResolveResult:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise DisputeDecisionReasonRequiredError("Admin reason is required.")

    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise DisputeCaseNotFoundError("Dispute not found.")

    transaction = db.get(Transaction, dispute.transaction_id)
    if transaction is None:
        raise DisputeCaseNotFoundError("Transaction for dispute not found.")

    if dispute.status != DisputeStatus.ESCALATED_ADMIN_REVIEW:
        raise DisputeCaseInvalidStateError(
            "Dispute must be in escalated_admin_review for admin force resolve."
        )

    if transaction.status != TransactionStatus.ESCALATED_ADMIN_REVIEW:
        raise DisputeCaseInvalidStateError(
            "Transaction must be in escalated_admin_review for admin force resolve."
        )

    now = datetime.now(UTC)
    prior_transaction_status = transaction.status
    prior_dispute_status = dispute.status
    escrow_service = EscrowService(
        db,
        actor_type=ACTOR_ADMIN,
        actor_id=actor_id,
        audit_reason=normalized_reason,
    )

    transaction.released_at = None
    transaction.refunded_at = None
    dispute.split_ratio = None

    if decision == AdminDecision.REFUND:
        transaction.status = TransactionStatus.RESOLVED_REFUND
        transaction.refunded_at = now
        dispute.status = DisputeStatus.RESOLVED_REFUND
        escrow_service.queue_refund_full(
            transaction,
            purpose=f"admin-refund:{dispute.id}",
        )
    elif decision == AdminDecision.RELEASE:
        transaction.status = TransactionStatus.RESOLVED_RELEASE
        transaction.released_at = now
        dispute.status = DisputeStatus.RESOLVED_RELEASE
        escrow_service.queue_release_full(
            transaction,
            purpose=f"admin-release:{dispute.id}",
        )
    else:
        split_ratio = Decimal("0.5000")
        try:
            escrow_service.queue_split_payout(
                transaction,
                release_purpose=f"admin-split-release:{dispute.id}",
                refund_purpose=f"admin-split-refund:{dispute.id}",
                split_ratio=split_ratio,
            )
        except SplitPayoutUnsupportedError as exc:
            raise DisputeSplitPayoutUnsupportedError(str(exc)) from exc

        transaction.status = TransactionStatus.RESOLVED_SPLIT
        transaction.released_at = now
        transaction.refunded_at = now
        dispute.status = DisputeStatus.RESOLVED_SPLIT
        dispute.split_ratio = split_ratio

    dispute.admin_decision = decision
    dispute.admin_reason = normalized_reason
    dispute.resolved_at = now

    record_money_audit_event(
        db,
        action=f"admin_force_resolve_{decision.value}",
        actor_type=ACTOR_ADMIN,
        actor_id=actor_id,
        reason=normalized_reason,
        transaction_id=transaction.id,
        amount=transaction.amount,
        details={
            "dispute_id": str(dispute.id),
            "split_ratio": str(dispute.split_ratio) if dispute.split_ratio else None,
        },
        occurred_at=now,
    )

    _create_admin_decision_notifications(
        db,
        transaction=transaction,
        dispute=dispute,
        reason=normalized_reason,
        prior_transaction_status=prior_transaction_status,
        prior_dispute_status=prior_dispute_status,
    )

    db.commit()
    db.refresh(transaction)
    db.refresh(dispute)

    return AdminForceResolveResult(
        dispute_id=dispute.id,
        transaction_id=transaction.id,
        decision=decision,
        reason=normalized_reason,
        dispute_status=dispute.status,
        transaction_status=transaction.status,
        resolved_at=dispute.resolved_at or now,
        released_at=transaction.released_at,
        refunded_at=transaction.refunded_at,
    )


def _create_admin_decision_notifications(
    db: Session,
    *,
    transaction: Transaction,
    dispute: Dispute,
    reason: str,
    prior_transaction_status: TransactionStatus,
    prior_dispute_status: DisputeStatus,
) -> None:
    payload = {
        "decision": dispute.admin_decision.value if dispute.admin_decision else None,
        "reason": reason,
        "dispute_id": str(dispute.id),
        "transition": {
            "transaction": {
                "from": prior_transaction_status.value,
                "to": transaction.status.value,
            },
            "dispute": {
                "from": prior_dispute_status.value,
                "to": dispute.status.value,
            },
        },
    }
    title = "Admin dispute decision"
    message = f"Dispute was resolved by admin as {dispute.admin_decision.value}."

    for recipient_id in (transaction.buyer_id, transaction.seller_id):
        db.add(
            Notification(
                user_id=recipient_id,
                transaction_id=transaction.id,
                event_type=NotificationEventType.ADMIN_DECISION,
                title=title,
                message=message,
                payload=payload,
            )
        )
