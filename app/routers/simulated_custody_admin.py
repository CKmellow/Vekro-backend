from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.simulated_custody import (
    AdminCollectionControlResponse,
    AdminForceCompleteCollectionRequest,
    AdminPayoutControlResponse,
)
from app.services.custody.simulated_admin import (
    SimulatedAdminAttemptNotFoundError,
    SimulatedAdminEscrowNotFoundError,
    SimulatedAdminPayoutTypeError,
    force_complete_simulated_collection,
    progress_simulated_collection_scenario,
    progress_simulated_payout_scenario,
)
from app.services.custody.simulated_provider import (
    SimulatedEscrowNotFoundError,
    SimulatedEscrowReferenceError,
)

router = APIRouter(prefix="/admin/simulated-custody", tags=["Admin Simulated Custody"])


def _ensure_simulation_admin_controls_enabled() -> None:
    if get_settings().environment.strip().lower() == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Simulation admin controls are disabled in production.",
        )


@router.post(
    "/collections/force-complete",
    response_model=AdminCollectionControlResponse,
    status_code=status.HTTP_200_OK,
    summary="Force-complete simulated collection (admin, non-production only)",
    description=(
        "Simulation-only admin control that forces deterministic successful collection in demo "
        "environments. Endpoint is disabled in production and is idempotent across retries."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Authenticated user is not an admin or control is disabled in production."
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "Escrow reference not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Payload or escrow reference format is invalid."
        },
    },
)
def admin_force_complete_collection(
    payload: AdminForceCompleteCollectionRequest,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminCollectionControlResponse:
    _ = current_user
    _ensure_simulation_admin_controls_enabled()

    try:
        result = force_complete_simulated_collection(
            db,
            escrow_reference=payload.escrow_reference,
            amount=payload.amount,
            phone_number=payload.phone_number,
            account_reference=payload.account_reference,
            currency=payload.currency,
        )
    except SimulatedEscrowReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except (SimulatedEscrowNotFoundError, SimulatedAdminEscrowNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return AdminCollectionControlResponse.model_validate(result)


@router.post(
    "/collections/{provider_reference}/progress",
    response_model=AdminCollectionControlResponse,
    status_code=status.HTTP_200_OK,
    summary="Progress simulated collection scenario (admin, non-production only)",
    description=(
        "Simulation-only admin control that advances deterministic collection status flows "
        "(for example out_of_order reconciliation) while preserving idempotent ledger behavior."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Authenticated user is not an admin or control is disabled in production."
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "Collection attempt or escrow was not found."},
    },
)
def admin_progress_collection_scenario(
    provider_reference: str,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminCollectionControlResponse:
    _ = current_user
    _ensure_simulation_admin_controls_enabled()

    try:
        result = progress_simulated_collection_scenario(db, provider_reference=provider_reference)
    except (SimulatedAdminAttemptNotFoundError, SimulatedAdminEscrowNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return AdminCollectionControlResponse.model_validate(result)


@router.post(
    "/payouts/{provider_reference}/progress",
    response_model=AdminPayoutControlResponse,
    status_code=status.HTTP_200_OK,
    summary="Progress simulated payout scenario (admin, non-production only)",
    description=(
        "Simulation-only admin control that advances deterministic payout status flows in demo "
        "environments. Endpoint is disabled in production and preserves idempotent replay safety."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {
            "description": (
                "Authenticated user is not an admin or control is disabled in production."
            )
        },
        status.HTTP_404_NOT_FOUND: {"description": "Payout attempt or escrow was not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Payout attempt is missing release/refund metadata."
        },
    },
)
def admin_progress_payout_scenario(
    provider_reference: str,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminPayoutControlResponse:
    _ = current_user
    _ensure_simulation_admin_controls_enabled()

    try:
        result = progress_simulated_payout_scenario(db, provider_reference=provider_reference)
    except (SimulatedAdminAttemptNotFoundError, SimulatedAdminEscrowNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except SimulatedAdminPayoutTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return AdminPayoutControlResponse.model_validate(result)
