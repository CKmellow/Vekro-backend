import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateListingRequest(BaseModel):
    """Payload used by sellers to create listings."""

    title: str = Field(min_length=2, max_length=255, description="Listing title.")
    description: str = Field(default="", max_length=2000, description="What the buyer gets.")
    category: str | None = Field(default=None, max_length=64, description="Shop category.")
    buyer_name: str | None = Field(
        default=None,
        max_length=120,
        description="Optional: the one buyer this link is meant for.",
    )
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

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("category", "buyer_name", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("unique_id", mode="before")
    @classmethod
    def normalize_unique_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


ListingStatus = Literal["active", "awaiting_payment", "sold", "archived"]


class ListingPhotoResponse(BaseModel):
    """A listing photo. `url` is relative to the API root."""

    id: uuid.UUID
    url: str


class ListingSellerSummary(BaseModel):
    """What a buyer may see about the seller before paying."""

    name: str
    member_since: datetime
    completed_sales: int = Field(description="Transactions released to this seller.")


class ListingResponse(BaseModel):
    """Listing representation returned by create/get listing endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seller_id: uuid.UUID
    title: str
    description: str
    category: str | None
    buyer_name: str | None
    price: Decimal
    is_serialized: bool
    unique_id: str | None
    dispute_policy: dict[str, Any]
    status: ListingStatus = Field(
        description="active: open for payment. awaiting_payment: a buyer started paying. "
        "sold: paid and not refunded; new transactions are refused."
    )
    photos: list[ListingPhotoResponse] = Field(description="Ordered; the first is the cover.")
    seller: ListingSellerSummary
    created_at: datetime
    updated_at: datetime


class ListingStatusCounts(BaseModel):
    """How many of the seller's listings are in each status."""

    active: int
    awaiting_payment: int
    sold: int
    archived: int


class UpdateListingRequest(BaseModel):
    """Fields to change; anything left out keeps its value. The result must pass create rules."""

    title: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=64)
    buyer_name: str | None = Field(default=None, max_length=120)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    is_serialized: bool | None = None
    unique_id: str | None = Field(default=None, max_length=128)
    dispute_policy: dict[str, Any] | None = None


class ListingListResponse(BaseModel):
    """One page of the seller's own listings, newest first."""

    items: list[ListingResponse]
    total: int = Field(description="Listings matching the filters.")
    page: int
    page_size: int
    counts: ListingStatusCounts = Field(description="Per status, before filters.")
