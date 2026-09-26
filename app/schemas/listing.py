import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateListingRequest(BaseModel):
    """Payload used by sellers to create listings."""

    title: str = Field(min_length=2, max_length=255, description="Listing title.")
    price: Decimal = Field(
        ge=0,
        max_digits=12,
        decimal_places=2,
        description="Listing price amount.",
    )
    is_serialized: bool = Field(
        default=False,
        description="Whether listing requires serialized dispute/hold workflow.",
    )
    unique_id: str | None = Field(
        default=None,
        max_length=128,
        description="Required unique identifier when is_serialized is true.",
    )
    dispute_policy: dict[str, Any] = Field(
        default_factory=dict,
        description="Policy metadata that constrains dispute resolution actions.",
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be empty.")
        return normalized

    @field_validator("unique_id", mode="before")
    @classmethod
    def normalize_unique_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ListingResponse(BaseModel):
    """Listing representation returned by create/get listing endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seller_id: uuid.UUID
    title: str
    price: Decimal
    is_serialized: bool
    unique_id: str | None
    dispute_policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime
