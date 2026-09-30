from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.services.custody.enums import CollectionOutcome, PayoutOutcome


class AdminForceCompleteCollectionRequest(BaseModel):
    """Admin simulation control payload for forcing collection success."""

    escrow_reference: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ] = Field(description="Simulated escrow reference to fund.")
    amount: Decimal = Field(gt=0, description="Collection amount to post in demo mode.")
    phone_number: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ] = Field(description="Customer phone number used for simulated collection requests.")
    account_reference: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1),
    ] = Field(
        default=None,
        description=(
            "Optional operator reference. The endpoint prepends a success trigger to force "
            "deterministic successful funding."
        ),
    )
    currency: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=3),
    ] = Field(default="KES", description="Three-letter currency code for the collection amount.")


class AdminCollectionControlResponse(BaseModel):
    """Result payload for collection-focused simulated admin controls."""

    model_config = ConfigDict(from_attributes=True)

    escrow_reference: str
    provider_reference: str
    outcome: CollectionOutcome
    raw_status: str | None
    message: str | None
    idempotent_replay: bool


class AdminPayoutControlResponse(BaseModel):
    """Result payload for payout-focused simulated admin controls."""

    model_config = ConfigDict(from_attributes=True)

    escrow_reference: str
    provider_reference: str
    payout_type: str
    outcome: PayoutOutcome
    raw_status: str | None
    message: str | None
    idempotent_replay: bool
