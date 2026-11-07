from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.anchoring import AnchoringStatusResponse
from app.services.anchoring.status import anchoring_status

router = APIRouter(prefix="/admin/anchoring", tags=["Admin Anchoring"])


@router.get(
    "/status",
    response_model=AnchoringStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Anchoring worker status (admin only)",
    description=(
        "Whether anchoring is enabled, the chain and registry, the last confirmed batch, the batch "
        "in flight, how many recorded events still wait for a batch, and whether anchoring has "
        "halted because the chain disagreed with the database. Never shows the anchoring key."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
    },
)
def admin_anchoring_status(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AnchoringStatusResponse:
    _ = current_user
    return anchoring_status(db, get_settings())
