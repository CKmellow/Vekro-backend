import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.user import User
from app.services.manual_refunds import (
    ManualRefund,
    ManualRefundNotFoundError,
    ManualRefundRefusedError,
    complete_manual_refund,
    list_manual_refunds,
)

router = APIRouter(prefix="/admin/refunds", tags=["Admin"])


class ManualRefundResponse(BaseModel):
    attempt_id: uuid.UUID
    transaction_id: uuid.UUID
    reference: str = Field(description="The order reference, as on Pesapal (merchant reference).")
    amount: Decimal
    currency: str
    queued_at: datetime
    pesapal_order_tracking_id: str | None = Field(
        description="The Pesapal order that took the card payment; refund it in Pesapal."
    )
    status: Literal["pending", "completed"]

    @classmethod
    def of(cls, refund: ManualRefund) -> "ManualRefundResponse":
        return cls(**refund.__dict__)


class ManualRefundListResponse(BaseModel):
    items: list[ManualRefundResponse]


class CompleteManualRefundRequest(BaseModel):
    note: str = Field(
        min_length=3, max_length=500, description="What support did, for the audit trail."
    )
    provider_refund_reference: str | None = Field(
        default=None, max_length=120, description="Pesapal's reference for the refund, if any."
    )


@router.get(
    "/manual",
    response_model=ManualRefundListResponse,
    summary="Card refunds waiting for support (admin only)",
    description=(
        "Refunds of card-funded orders. Card money sits in the merchant's Pesapal account and is "
        "refunded by support in Pesapal (merchant approval, then Pesapal's processing), never "
        "over M-Pesa. Each item is marked 'manual refund required' in the order's record."
    ),
)
def admin_list_manual_refunds(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
    include_completed: bool = False,
) -> ManualRefundListResponse:
    _ = current_user
    return ManualRefundListResponse(
        items=[
            ManualRefundResponse.of(r)
            for r in list_manual_refunds(db, include_completed=include_completed)
        ]
    )


@router.post(
    "/{attempt_id}/complete",
    response_model=ManualRefundResponse,
    summary="Mark a card refund as done in Pesapal (admin only)",
    description=(
        "Records that support refunded the card payment in Pesapal. It settles the refund like "
        "an executed payout (ledger, escrow totals, payout status) and anchors "
        "'manual refund completed'. Idempotent. With ANCHOR_GATE_PAYOUTS it waits until the "
        "refund's approval is anchored."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "No manual refund with this id."},
        status.HTTP_409_CONFLICT: {
            "description": "Not completable yet: approval not anchored, or not enough held money."
        },
    },
)
def admin_complete_manual_refund(
    attempt_id: uuid.UUID,
    payload: CompleteManualRefundRequest,
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ManualRefundResponse:
    try:
        refund = complete_manual_refund(
            db,
            attempt_id=attempt_id,
            admin_id=current_user.id,
            note=payload.note.strip(),
            provider_refund_reference=payload.provider_refund_reference,
        )
    except ManualRefundNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ManualRefundRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ManualRefundResponse.of(refund)
