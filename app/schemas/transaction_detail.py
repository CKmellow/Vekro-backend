import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.core.phone import mask_phone
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.payment_prompt import PaymentInstructionResponse
from app.schemas.transaction import TransactionResponse
from app.services.deadlines import deadlines_for
from app.services.transaction_list import OrderListItem, OrderPage, OrderSegment
from app.services.transaction_payout import SellerPayout, SellerPayoutStatus
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

    @classmethod
    def for_transaction(cls, transaction: Transaction) -> "TransactionDeadlinesResponse":
        deadlines = deadlines_for(transaction)
        return cls(
            dispatch_by=deadlines.dispatch_by,
            inspect_by=deadlines.inspect_by,
            hold_until=deadlines.hold_until,
            return_send_by=deadlines.return_send_by,
            return_confirm_by=deadlines.return_confirm_by,
        )


class DoorRejection(BaseModel):
    """Why the buyer rejected the item at the door."""

    reason: str | None
    note: str | None


class SellerPayoutResponse(BaseModel):
    """Where the seller's money is for this order."""

    status: SellerPayoutStatus = Field(
        description=(
            "not_paid: the buyer hasn't paid. held: in escrow. in_progress: release queued or "
            "awaiting the provider. paid_out. delayed: the provider refused; retried or handled "
            "by support. refunded: the money went back to the buyer."
        )
    )
    amount: Decimal | None = Field(description="Released to the seller (gross), once releasing.")
    reference: str | None = Field(description="Provider reference of the succeeded release.")
    paid_at: datetime | None = Field(description="When the release succeeded.")

    @classmethod
    def from_payout(cls, payout: SellerPayout) -> "SellerPayoutResponse":
        return cls(
            status=payout.status,
            amount=payout.amount,
            reference=payout.reference,
            paid_at=payout.paid_at,
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
    door_rejection: DoorRejection | None = Field(
        default=None, description="Set when the buyer rejected the item at the door with a reason."
    )
    payment: PaymentInstructionResponse | None = Field(
        default=None,
        description="The latest payment prompt. Shown to the buyer and admins only.",
    )
    payout: SellerPayoutResponse | None = Field(
        default=None, description="The seller's payout. Shown to the seller and admins only."
    )

    @classmethod
    def from_view(
        cls,
        view: TransactionView,
        payment: PaymentInstructionResponse | None = None,
        payout: SellerPayout | None = None,
    ) -> "TransactionDetailResponse":
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
            deadlines=TransactionDeadlinesResponse.for_transaction(view.transaction),
            server_time=datetime.now(UTC),
            payment=payment if view.viewer_role != UserRole.SELLER else None,
            payout=(
                SellerPayoutResponse.from_payout(payout)
                if payout is not None and view.viewer_role != UserRole.BUYER
                else None
            ),
            door_rejection=(
                DoorRejection(
                    reason=view.transaction.door_rejection_reason,
                    note=view.transaction.door_rejection_note,
                )
                if view.transaction.door_rejection_reason or view.transaction.door_rejection_note
                else None
            ),
        )


def _party(user: User) -> TransactionParty:
    return TransactionParty(name=user.name, phone_masked=mask_phone(user.phone))


class TimelineEventResponse(BaseModel):
    """One step in an order's history, in plain words."""

    kind: str = Field(
        description="transaction_created or a notification event type, e.g. transaction_locked."
    )
    at: datetime
    title: str
    description: str


class TransactionSummaryListing(BaseModel):
    id: uuid.UUID
    title: str
    is_serialized: bool
    photo_url: str | None = Field(description="The listing's cover photo, when it has one.")


class TransactionSummaryResponse(BaseModel):
    """One row of the caller's order list."""

    id: uuid.UUID
    reference: str
    status: TransactionStatus
    amount: Decimal
    viewer_role: UserRole
    listing: TransactionSummaryListing
    counterpart: TransactionParty = Field(
        description="The other party: the buyer for a seller, the seller for a buyer."
    )
    deadlines: TransactionDeadlinesResponse
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_item(cls, item: OrderListItem, viewer_role: UserRole) -> "TransactionSummaryResponse":
        transaction = item.transaction
        return cls(
            id=transaction.id,
            reference=transaction.reference,
            status=transaction.status,
            amount=transaction.amount,
            viewer_role=viewer_role,
            listing=TransactionSummaryListing(
                id=item.listing.id,
                title=item.listing.title,
                is_serialized=item.listing.is_serialized,
                photo_url=(
                    f"/listings/{item.listing.id}/photos/{item.cover_photo_id}"
                    if item.cover_photo_id
                    else None
                ),
            ),
            counterpart=_party(item.counterpart),
            deadlines=TransactionDeadlinesResponse.for_transaction(transaction),
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
        )


class OrderSegmentCounts(BaseModel):
    """How many of the caller's orders are in each list segment."""

    needs_action: int
    in_transit: int
    held: int
    completed: int
    issues: int


class TransactionListResponse(BaseModel):
    """One page of the caller's orders, with counts across all of them."""

    items: list[TransactionSummaryResponse]
    total: int = Field(description="Orders matching the filters.")
    page: int
    page_size: int
    counts: OrderSegmentCounts = Field(description="Per segment, before filters.")
    status_counts: dict[str, int] = Field(
        description="Per transaction status, before filters. Missing statuses are 0."
    )
    server_time: datetime = Field(description="Server clock; offset countdowns with it.")

    @classmethod
    def from_page(cls, page: OrderPage) -> "TransactionListResponse":
        return cls(
            items=[TransactionSummaryResponse.from_item(i, page.viewer_role) for i in page.items],
            total=page.total,
            page=page.page,
            page_size=page.page_size,
            counts=OrderSegmentCounts(
                **{segment.value: page.segment_counts[segment] for segment in OrderSegment}
            ),
            status_counts={status.value: n for status, n in page.status_counts.items()},
            server_time=datetime.now(UTC),
        )
