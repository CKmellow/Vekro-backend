import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.admin_case import AdminCaseDetailResponse, AdminCaseListResponse
from app.services.admin_cases import (
    AdminCaseNotFoundError,
    CaseStatusFilter,
    get_admin_case,
    list_admin_cases,
)

router = APIRouter(prefix="/admin/cases", tags=["Admin Disputes"])

_ADMIN = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
    status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
}


@router.get(
    "",
    response_model=AdminCaseListResponse,
    summary="Support queue: escalated cases with counters",
    description=(
        "Open cases oldest first (by escalation), decided cases newest first, or all. Each case "
        "carries its reference, amount, item, parties and why it reached support."
    ),
    responses=_ADMIN,
)
def list_cases(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[CaseStatusFilter, Query(alias="status")] = CaseStatusFilter.OPEN,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AdminCaseListResponse:
    _ = current_user
    return AdminCaseListResponse.from_page(
        list_admin_cases(db, status=status_filter, page=page, page_size=page_size)
    )


@router.get(
    "/{dispute_id}",
    response_model=AdminCaseDetailResponse,
    summary="One case for review and decision",
    description=(
        "Parties with full phones and history, the item and declared policy, both accounts, the "
        "escalation reason, the decision when made, and a timeline (payloads are never returned)."
    ),
    responses={**_ADMIN, status.HTTP_404_NOT_FOUND: {"description": "Case not found."}},
)
def get_case(
    dispute_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminCaseDetailResponse:
    _ = current_user
    try:
        return AdminCaseDetailResponse.from_detail(get_admin_case(db, dispute_id))
    except AdminCaseNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Case not found.") from exc
