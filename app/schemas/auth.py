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
    google_email: str | None = Field(
        default=None, description="The linked Google account's e-mail, for display (#324)."
    )
    has_password: bool = Field(
        default=True,
        description="False for a seller who signed up with Google and has not set a password yet.",
    )


class PhoneCodeRequest(BaseModel):
    """Ask for a one-time sign-in code by SMS."""

    phone: str = Field(min_length=7, max_length=32, description="Buyer's phone number.")

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str) -> str | None:
        return normalize_phone(value)


class PhoneCodeRequestResponse(BaseModel):
    """The code is on its way."""

    resend_after_seconds: int = Field(description="Seconds before another code can be requested.")
    expires_at: datetime = Field(description="When this code stops working.")
    dev_code: str | None = Field(
        default=None,
        description="The code itself, only when PHONE_CODE_DEV_ECHO is on (never in production).",
    )


class PhoneCodeVerifyRequest(BaseModel):
    """Verify the SMS code and open a buyer session."""

    phone: str = Field(min_length=7, max_length=32, description="Buyer's phone number.")
    code: str = Field(pattern=r"^\d{6}$", description="The 6-digit code from the SMS.")

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str) -> str | None:
        return normalize_phone(value)


class GoogleStatusResponse(BaseModel):
    """Whether Continue with Google is offered (#324)."""

    enabled: bool


class GoogleLinkRequest(BaseModel):
    """Start linking Google to the signed-in account (#324)."""

    return_to: str | None = Field(
        default=None,
        max_length=200,
        description="Path on this site to come back to, e.g. /seller/settings.",
    )


class GoogleLinkResponse(BaseModel):
    authorization_url: str = Field(description="Send the browser here (Google's consent screen).")


class GoogleSignupResponse(BaseModel):
    """The Google identity waiting for a verified phone."""

    email: str
    name: str | None


class GoogleSignupCodeRequest(BaseModel):
    phone: str = Field(
        min_length=7, max_length=32, description="The seller's Kenyan mobile number."
    )


class GoogleSignupCompleteRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    code: str = Field(pattern=r"^\d{6}$", description="The 6-digit code from the SMS.")
    name: str | None = Field(
        default=None, max_length=120, description="Shown to buyers; defaults to the Google name."
    )
