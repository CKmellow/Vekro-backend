import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.dispute import (
    AdminForceResolveRequest,
    AdminForceResolveResponse,
    DisputeCaseTimelineResponse,
    EscalationQueueItemResponse,
)
from app.services.dispute import (
    DisputeCaseInvalidStateError,
    DisputeCaseNotFoundError,
    DisputeDecisionReasonRequiredError,
    force_resolve_dispute_case,
    get_dispute_case_timeline,
    list_escalated_disputes,
)

router = APIRouter(prefix="/admin/disputes", tags=["Admin Disputes"])


@router.get(
    "/escalation-queue",
    response_model=list[EscalationQueueItemResponse],
    status_code=status.HTTP_200_OK,
    summary="List escalated disputes awaiting admin review",
    description=(
        "Admin-only queue for disputes currently in escalated_admin_review state, "
        "including buyer/seller/listing triage context."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
    },
)
def list_admin_escalation_queue(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[EscalationQueueItemResponse]:
    _ = current_user
    queue_items = list_escalated_disputes(db)
    return [EscalationQueueItemResponse.model_validate(item) for item in queue_items]


@router.get(
    "/{dispute_id}/timeline",
    response_model=DisputeCaseTimelineResponse,
    status_code=status.HTTP_200_OK,
    summary="Get admin dispute case timeline detail",
    description=(
        "Admin-only case view that combines dispute metadata, transaction context, "
        "and chronologically ordered timeline events."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
        status.HTTP_404_NOT_FOUND: {"description": "Dispute case not found."},
    },
)
def get_admin_dispute_case_timeline(
    dispute_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DisputeCaseTimelineResponse:
    _ = current_user
    try:
        case = get_dispute_case_timeline(db, dispute_id=dispute_id)
    except DisputeCaseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dispute case not found.",
        ) from exc

    return DisputeCaseTimelineResponse.model_validate(case)


@router.post(
    "/{dispute_id}/force-resolve",
    response_model=AdminForceResolveResponse,
    status_code=status.HTTP_200_OK,
    summary="Force resolve an escalated dispute case",
    description=(
        "Admin-only terminal resolution action for escalated disputes. "
        "Applies refund/release/split decision and persists reason/decision metadata."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not an admin."},
        status.HTTP_404_NOT_FOUND: {"description": "Dispute case not found."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Dispute state is not eligible or reason/decision is invalid."
        },
    },
)
def admin_force_resolve_dispute_case(
    dispute_id: uuid.UUID,
    payload: AdminForceResolveRequest,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminForceResolveResponse:
    _ = current_user
    try:
        result = force_resolve_dispute_case(
            db,
            dispute_id=dispute_id,
            decision=payload.decision,
            reason=payload.reason,
        )
    except DisputeCaseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dispute case not found.",
        ) from exc
    except DisputeDecisionReasonRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except DisputeCaseInvalidStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return AdminForceResolveResponse.model_validate(result)
