from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.rail_health import AdminRailHealthResponse
from app.services.custody.rail_breaker import build_admin_rail_health_payload
from app.services.custody.registry import CustodyRegistry

router = APIRouter(prefix="/admin/rails", tags=["Admin Rails"])


@router.get(
    "/health",
    response_model=AdminRailHealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Inspect custody rail breaker health",
    description=(
        "Returns custody mode, provider capability signals, and per-rail circuit breaker "
        "state used for routing decisions."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": "Authenticated user does not have admin role."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Custody registry is unavailable in app state."
        },
    },
)
def admin_rail_health(
    request: Request,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminRailHealthResponse:
    _ = current_user
    registry = getattr(request.app.state, "custody_registry", None)
    if not isinstance(registry, CustodyRegistry):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Custody registry is unavailable.",
        )

    payload = build_admin_rail_health_payload(db, registry=registry)
    return AdminRailHealthResponse.model_validate(payload)
