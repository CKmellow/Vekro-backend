import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationChannel
from app.models.transaction import Transaction


class NotificationNotFoundError(Exception):
    """The notification does not exist or belongs to someone else."""


def list_user_notifications(
    db: Session,
    *,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
    unread_only: bool = False,
    channel: NotificationChannel | None = None,
) -> list[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.is_read.is_(False))
    if channel is not None:
        query = query.where(Notification.channel == channel)
    notifications = list(
        db.execute(
            query.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    # Defensive filter to prevent accidental cross-user leakage.
    filtered = [item for item in notifications if item.user_id == user_id]
    filtered.sort(key=lambda item: (item.created_at, item.id), reverse=True)
    return filtered


def count_unread_in_app(db: Session, *, user_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.is_read.is_(False),
            )
        ).scalar_one()
    )


def mark_notification_read(
    db: Session, *, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    """Idempotent: a notification already read keeps its first read_at."""
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotificationNotFoundError("Notification not found.")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_notifications_read(db: Session, *, user_id: uuid.UUID) -> int:
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def transaction_references(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Human references for these transactions, in one query."""
    if not ids:
        return {}
    return dict(
        db.execute(
            select(Transaction.id, Transaction.reference).where(Transaction.id.in_(ids))
        ).all()
    )
