import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.transaction import TransactionStatus


class CreateTransactionRequest(BaseModel):
    listing_id: uuid.UUID
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class TransactionResponse(BaseModel):
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
    transaction_id: uuid.UUID
    result_code: int
    result_desc: str | None = Field(default=None, max_length=255)
    checkout_request_id: str | None = Field(default=None, max_length=120)
    merchant_request_id: str | None = Field(default=None, max_length=120)
    provider_reference: str | None = Field(default=None, max_length=120)


class PaymentCallbackResponse(BaseModel):
    transaction_id: uuid.UUID
    status: TransactionStatus
    transitioned: bool
    duplicate: bool
    detail: str


class OtpGiveRequest(BaseModel):
    otp_code: str = Field(min_length=4, max_length=12, pattern=r"^\d+$")


class ReportFunctionalIssueRequest(BaseModel):
    category: str = Field(min_length=2, max_length=64)
    description: str = Field(min_length=5, max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class SellerResolutionActionRequest(BaseModel):
    action: str = Field(min_length=3, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
