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
