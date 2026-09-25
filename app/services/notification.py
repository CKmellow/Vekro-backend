import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import Notification


def list_user_notifications(
    db: Session,
    *,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
) -> list[Notification]:
    notifications = list(
        db.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
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
