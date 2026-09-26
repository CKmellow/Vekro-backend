import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.transaction import TransactionStatus


class CreateTransactionRequest(BaseModel):
    """Buyer payload for creating a new escrow transaction."""

    listing_id: uuid.UUID
    amount: Decimal = Field(
        gt=0,
        max_digits=12,
        decimal_places=2,
        description="Escrow amount to lock for the transaction.",
    )


class TransactionResponse(BaseModel):
    """Current transaction state and ownership metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    amount: Decimal
    status: TransactionStatus
    created_at: datetime
    updated_at: datetime


class PaymentCallbackRequest(BaseModel):
    """Payment provider callback payload used to reconcile transaction payment state."""

    transaction_id: uuid.UUID
    result_code: int = Field(description="Provider result code (0 means successful payment).")
    result_desc: str | None = Field(
        default=None,
        max_length=255,
        description="Provider result description.",
    )
    checkout_request_id: str | None = Field(
        default=None,
        max_length=120,
        description="Provider checkout request identifier.",
    )
    merchant_request_id: str | None = Field(
        default=None,
        max_length=120,
        description="Provider merchant request identifier.",
    )
    provider_reference: str | None = Field(
        default=None,
        max_length=120,
        description="Optional provider transaction/reference id.",
    )


class PaymentCallbackResponse(BaseModel):
    """Outcome of callback processing and resulting transaction state."""

    transaction_id: uuid.UUID
    status: TransactionStatus
    transitioned: bool
    duplicate: bool
    detail: str


class OtpGiveRequest(BaseModel):
    """Buyer OTP payload for confirming delivery handoff."""

    otp_code: str = Field(
        min_length=4,
        max_length=12,
        pattern=r"^\d+$",
        description="Numeric OTP code shared at delivery handoff.",
    )


class ReportFunctionalIssueRequest(BaseModel):
    """Buyer dispute intake payload used during serialized hold window."""

    category: str = Field(
        min_length=2,
        max_length=64,
        description="Issue category (for example not_working, missing_parts, other).",
    )
    description: str = Field(
        min_length=5,
        max_length=2000,
        description="Detailed issue report supplied by the buyer.",
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Evidence metadata payload (links, notes, file references).",
    )


class SellerResolutionActionRequest(BaseModel):
    """Seller-selected resolution action in dispute flow."""

    action: str = Field(
        min_length=3,
        max_length=64,
        description="Resolution action key, such as refund_issued or repair_shipped.",
    )
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional seller notes for the selected action.",
    )


class BuyerReconfirmationRequest(BaseModel):
    """Buyer response after seller remediation action."""

    accepted: bool
    notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional buyer remarks supporting acceptance or rejection.",
    )
