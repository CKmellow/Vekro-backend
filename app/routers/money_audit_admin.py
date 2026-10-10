import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.money_audit import MoneyAuditEventListResponse, MoneyAuditEventResponse
from app.services.audit import MoneyAuditQuery, list_money_audit_events

router = APIRouter(prefix="/admin/audit", tags=["Admin Audit"])


@router.get(
    "/money-events",
    response_model=MoneyAuditEventListResponse,
    status_code=status.HTTP_200_OK,
    summary="Review money-movement audit events (admin only)",
    description=(
        "Lists audit records for payouts, funding confirmations, and admin money decisions, "
        "newest first. Filter by transaction, provider reference, rail, or action."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
    },
)
def admin_list_money_audit_events(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
    transaction_id: uuid.UUID | None = None,
    provider_reference: Annotated[str | None, Query(max_length=120)] = None,
    rail_name: Annotated[str | None, Query(max_length=40)] = None,
    action: Annotated[str | None, Query(max_length=60)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MoneyAuditEventListResponse:
    _ = current_user
    events = list_money_audit_events(
        db,
        MoneyAuditQuery(
            transaction_id=transaction_id,
            provider_reference=provider_reference,
            rail_name=rail_name,
            action=action,
            limit=limit,
            offset=offset,
        ),
    )
    return MoneyAuditEventListResponse(
        items=[MoneyAuditEventResponse.model_validate(event) for event in events],
        limit=limit,
        offset=offset,
    )
