from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.phone import normalize_phone


class SellerProfileResponse(BaseModel):
    """The signed-in seller's shop profile and payout details (their own, so unmasked)."""

    name: str
    phone: str
    shop_name: str | None
    shop_area: str | None
    shop_about: str | None
    logo_url: str | None = Field(description="Public logo URL, or null without a logo.")
    mpesa_phone: str = Field(description="Where payouts go (the login phone if none was set).")
    mpesa_account_name: str | None
    has_orders: bool = Field(
        description="False before the first order: a new payout number needs no SMS code yet."
    )
    updated_at: datetime


class SellerProfileUpdate(BaseModel):
    """Only the fields sent are changed; an empty string clears a field."""

    shop_name: str | None = Field(default=None, min_length=2, max_length=60)
    shop_area: str | None = Field(default=None, max_length=80)
    shop_about: str | None = Field(default=None, max_length=280)


class PayoutCodeRequest(BaseModel):
    mpesa_phone: str = Field(min_length=7, max_length=32)

    @field_validator("mpesa_phone", mode="before")
    @classmethod
    def normalize(cls, value: str) -> str | None:
        return normalize_phone(value)


class PayoutUpdate(BaseModel):
    mpesa_phone: str = Field(min_length=7, max_length=32)
    mpesa_account_name: str = Field(min_length=2, max_length=120)
    code: str | None = Field(default=None, pattern=r"^\d{6}$")

    @field_validator("mpesa_phone", mode="before")
    @classmethod
    def normalize(cls, value: str) -> str | None:
        return normalize_phone(value)
