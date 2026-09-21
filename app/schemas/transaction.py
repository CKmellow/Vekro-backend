import uuid
from datetime import datetime
from decimal import Decimal

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
