import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.phone import normalize_phone


def _strong(value: str) -> str:
    if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        raise ValueError("Use 8 or more characters with a letter and a number.")
    return value


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strong(cls, value: str) -> str:
        return _strong(value)


class PasswordChangeResponse(BaseModel):
    signed_out_devices: int = Field(description="Other devices signed out by the change.")


class ResetCodeRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=32)

    @field_validator("phone", mode="before")
    @classmethod
    def normalize(cls, value: str) -> str | None:
        return normalize_phone(value)


class ResetCodeResponse(BaseModel):
    resend_after_seconds: int
    dev_code: str | None = Field(
        default=None, description="Only with PHONE_CODE_DEV_ECHO on (never in production)."
    )


class PasswordReset(BaseModel):
    phone: str = Field(min_length=7, max_length=32)
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("phone", mode="before")
    @classmethod
    def normalize(cls, value: str) -> str | None:
        return normalize_phone(value)

    @field_validator("new_password")
    @classmethod
    def strong(cls, value: str) -> str:
        return _strong(value)


class DeviceSessionResponse(BaseModel):
    id: uuid.UUID
    device: str = Field(description='From the User-Agent, such as "Chrome on Android".')
    created_at: datetime
    last_seen_at: datetime
    current: bool = Field(description="This device.")


class RevokedResponse(BaseModel):
    revoked: int


class ChannelPrefs(BaseModel):
    in_app: bool
    sms: bool


class NotificationPreferences(BaseModel):
    """In-app and SMS per category. support.in_app is always true."""

    payment_received: ChannelPrefs
    delivery: ChannelPrefs
    returns: ChannelPrefs
    support: ChannelPrefs
