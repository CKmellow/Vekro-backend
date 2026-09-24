from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.dispute import EscalationQueueItemResponse
from app.services.dispute import list_escalated_disputes

router = APIRouter(prefix="/admin/disputes", tags=["Admin Disputes"])


@router.get(
    "/escalation-queue",
    response_model=list[EscalationQueueItemResponse],
    status_code=status.HTTP_200_OK,
    summary="List escalated disputes awaiting admin review",
)
def list_admin_escalation_queue(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[EscalationQueueItemResponse]:
    _ = current_user
    queue_items = list_escalated_disputes(db)
    return [EscalationQueueItemResponse.model_validate(item) for item in queue_items]
