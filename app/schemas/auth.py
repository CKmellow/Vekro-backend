import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.phone import normalize_phone
from app.models.user import UserRole


class RegisterUserRequest(BaseModel):
    """Payload for buyer/seller registration."""

    name: str = Field(min_length=2, max_length=120, description="Display name for the account.")
    phone: str = Field(
        min_length=7,
        max_length=32,
        description="Primary login phone number in digits with optional leading +.",
    )
    role: UserRole
    password: str = Field(min_length=8, max_length=128, description="Account password.")
    mpesa_phone: str | None = Field(
        default=None,
        max_length=32,
        description="Optional payout/charge phone for M-Pesa flows.",
    )
    mpesa_account_name: str | None = Field(
        default=None,
        max_length=120,
        description="Optional account holder name associated with M-Pesa phone.",
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: UserRole) -> UserRole:
        if value not in {UserRole.BUYER, UserRole.SELLER}:
            raise ValueError("Only buyer and seller registration is supported.")
        return value

    @field_validator("phone", "mpesa_phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value, allow_none=value is None)


class RegisterUserResponse(BaseModel):
    """Registered user profile returned after account creation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone: str
    role: UserRole
    mpesa_phone: str | None
    mpesa_account_name: str | None
    is_active: bool
    created_at: datetime


class LoginRequest(BaseModel):
    """Credentials payload for session-based authentication."""

    phone: str = Field(
        min_length=7,
        max_length=32,
        description="Account phone number used for login.",
    )
    password: str = Field(min_length=8, max_length=128, description="Account password.")

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)


class LoginResponse(BaseModel):
    """Authenticated user profile plus session expiry metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone: str
    role: UserRole
    mpesa_phone: str | None
    mpesa_account_name: str | None
    is_active: bool
    created_at: datetime
    session_expires_at: datetime
