import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth_context import get_current_user
from app.db.session import get_db
from app.models.notification import NotificationChannel
from app.models.user import User
from app.schemas.notification import NotificationResponse
from app.services.notification import (
    NotificationNotFoundError,
    count_unread_in_app,
    list_user_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class UnreadCountResponse(BaseModel):
    """Unread in-app notifications, for the bell badge."""

    unread_count: int


class MarkAllReadResponse(BaseModel):
    """How many notifications were marked read, and what is still unread (always 0)."""

    updated: int
    unread_count: int


@router.get(
    "",
    response_model=list[NotificationResponse],
    status_code=status.HTTP_200_OK,
    summary="List notifications for current user",
    description=(
        "Return notifications belonging to the authenticated user only, ordered by newest first. "
        "Supports limit/offset pagination for client polling, an unread-only filter and a "
        "channel filter (in-app inboxes pass channel=in_app so SMS copies are not repeated)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Pagination query parameters are invalid."
        },
    },
)
def list_current_user_notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    unread: Annotated[bool, Query(description="Only unread notifications.")] = False,
    channel: Annotated[NotificationChannel | None, Query(description="Only this channel.")] = None,
) -> list[NotificationResponse]:
    notifications = list_user_notifications(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        unread_only=unread,
        channel=channel,
    )
    return [NotificationResponse.model_validate(item) for item in notifications]


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="Count unread in-app notifications",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."}},
)
def get_unread_count(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UnreadCountResponse:
    return UnreadCountResponse(unread_count=count_unread_in_app(db, user_id=current_user.id))


@router.post(
    "/read-all",
    response_model=MarkAllReadResponse,
    summary="Mark all my notifications as read",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."}},
)
def mark_all_read(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MarkAllReadResponse:
    updated = mark_all_notifications_read(db, user_id=current_user.id)
    return MarkAllReadResponse(updated=updated, unread_count=0)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationResponse,
    summary="Mark one notification as read",
    description="Idempotent: a notification already read keeps its first read_at.",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_404_NOT_FOUND: {"description": "Not found, or not the caller's."},
    },
)
def mark_one_read(
    notification_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NotificationResponse:
    try:
        notification = mark_notification_read(
            db, user_id=current_user.id, notification_id=notification_id
        )
    except NotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
        ) from exc
    return NotificationResponse.model_validate(notification)
