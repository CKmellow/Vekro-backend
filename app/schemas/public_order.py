from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.listing import ListingStatus


class PublicOrderSummary(BaseModel):
    """What an order link shows before the buyer verifies their phone: no personal data."""

    reference: str = Field(description="Human order reference, such as VK-7K3P9Q.")
    listing_title: str
    amount: Decimal
    state_group: Literal["unpaid", "paid", "closed"] = Field(
        description=(
            "unpaid: waiting for M-Pesa; paid: money in escrow; closed: released or refunded."
        )
    )
    listing_status: ListingStatus


class PublicCardPaymentStatus(BaseModel):
    """What the card return page shows without a session (#308): status only, no personal data,
    no ids and no amounts."""

    reference: str = Field(description="Human order reference, such as VK-7K3P9Q.")
    state_group: Literal["unpaid", "paid", "closed"] = Field(
        description="As in the public order summary: paid once the payment is confirmed."
    )
    payment_status: Literal["pending", "failed", "succeeded"] = Field(
        description=(
            "This card checkout: pending (not confirmed yet), failed (declined or refused; a "
            "declined card can still be retried on Pesapal's page) or succeeded."
        )
    )
    failure_reason: Literal["declined", "provider_error"] | None = Field(
        default=None, description="Why it failed: declined by the card, or refused by Vekro."
    )
