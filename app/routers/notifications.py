from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.notification import NotificationResponse
from app.services.notification import list_user_notifications

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get(
    "",
    response_model=list[NotificationResponse],
    status_code=status.HTTP_200_OK,
    summary="List notifications for current user",
    description=(
        "Return notifications belonging to the authenticated user only, ordered by newest first. "
        "Supports limit/offset pagination for client polling."
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
) -> list[NotificationResponse]:
    notifications = list_user_notifications(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return [NotificationResponse.model_validate(item) for item in notifications]
