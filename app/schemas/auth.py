import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import UserRole

PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")


def _normalize_phone_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().replace(" ", "")
    if not PHONE_PATTERN.fullmatch(normalized):
        raise ValueError("Phone number must contain 7-15 digits and optional leading +.")
    return normalized


class RegisterUserRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=7, max_length=32)
    role: UserRole
    password: str = Field(min_length=8, max_length=128)
    mpesa_phone: str | None = Field(default=None, max_length=32)
    mpesa_account_name: str | None = Field(default=None, max_length=120)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: UserRole) -> UserRole:
        if value not in {UserRole.BUYER, UserRole.SELLER}:
            raise ValueError("Only buyer and seller registration is supported.")
        return value

    @field_validator("phone", "mpesa_phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return _normalize_phone_value(value)


class RegisterUserResponse(BaseModel):
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
    phone: str = Field(min_length=7, max_length=32)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return _normalize_phone_value(value)


class LoginResponse(BaseModel):
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
