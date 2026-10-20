import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.core.phone import mask_phone
from app.models.user import User, UserRole
from app.schemas.transaction import TransactionResponse
from app.services.deadlines import deadlines_for
from app.services.transaction_read import TransactionView


class TransactionListingSummary(BaseModel):
    """The listing the transaction was created from."""

    id: uuid.UUID
    title: str
    price: Decimal
    is_serialized: bool
    dispute_policy: dict[str, Any]


class TransactionParty(BaseModel):
    """A party to the transaction, safe to show to the other party."""

    name: str
    phone_masked: str | None = Field(description="Masked phone, for example +254 712 *** 678.")


class TransactionDeadlinesResponse(BaseModel):
    """When the current state times out (UTC). Only the field for the current state is set."""

    dispatch_by: datetime | None = Field(
        default=None,
        description="locked: the seller must dispatch by then or the buyer is refunded.",
    )
    inspect_by: datetime | None = Field(
        default=None,
        description="at_door_pending_inspection: no decision by then withholds the code.",
    )
    hold_until: datetime | None = Field(
        default=None, description="hold_24h: payment is released to the seller at this time."
    )
    return_send_by: datetime | None = Field(
        default=None, description="disputed_functional: the buyer must send the item back by then."
    )
    return_confirm_by: datetime | None = Field(
        default=None,
        description="return_in_transit: the seller must confirm receipt by then.",
    )


class TransactionDetailResponse(TransactionResponse):
    """One transaction as its buyer, its seller or an admin sees it."""

    viewer_role: UserRole = Field(description="How the caller relates to this transaction.")
    listing: TransactionListingSummary
    buyer: TransactionParty
    seller: TransactionParty
    deadlines: TransactionDeadlinesResponse
    server_time: datetime = Field(
        description="Server clock when the response was built; offset countdowns with it."
    )

    @classmethod
    def from_view(cls, view: TransactionView) -> "TransactionDetailResponse":
        deadlines = deadlines_for(view.transaction)
        base = TransactionResponse.model_validate(view.transaction)
        return cls(
            **base.model_dump(),
            viewer_role=view.viewer_role,
            listing=TransactionListingSummary(
                id=view.listing.id,
                title=view.listing.title,
                price=view.listing.price,
                is_serialized=view.listing.is_serialized,
                dispute_policy=view.listing.dispute_policy or {},
            ),
            buyer=_party(view.buyer),
            seller=_party(view.seller),
            deadlines=TransactionDeadlinesResponse(
                dispatch_by=deadlines.dispatch_by,
                inspect_by=deadlines.inspect_by,
                hold_until=deadlines.hold_until,
                return_send_by=deadlines.return_send_by,
                return_confirm_by=deadlines.return_confirm_by,
            ),
            server_time=datetime.now(UTC),
        )


def _party(user: User) -> TransactionParty:
    return TransactionParty(name=user.name, phone_masked=mask_phone(user.phone))
