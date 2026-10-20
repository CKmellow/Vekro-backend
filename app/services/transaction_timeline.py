"""Order timeline for the buyer, the seller and admins, built from the caller's notifications.

Only the event type, the time and fixed copy written here are returned. Notification messages and
payloads are never exposed: the buyer's arrival notification carries the delivery code.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import Notification, NotificationEventType
from app.models.user import User, UserRole
from app.services.transaction_read import get_transaction_for_viewer

TRANSACTION_CREATED = "transaction_created"


@dataclass(frozen=True)
class TimelineEvent:
    kind: str
    at: datetime
    title: str
    description: str


# (title, description) for buyer and seller, keyed by the server's own notification titles.
_BY_TITLE: dict[str, tuple[str, str, str]] = {
    # title -> (timeline title, buyer description, seller description)
    "Transaction created": (
        "Order created",
        "Waiting for your payment.",
        "Waiting for the buyer to pay.",
    ),
    "New escrow transaction": (
        "Order created",
        "Waiting for your payment.",
        "Waiting for the buyer to pay.",
    ),
    "Payment confirmed": (
        "Paid into the vault",
        "Vekro holds your money.",
        "Vekro holds the buyer's money.",
    ),
    "Buyer payment confirmed": (
        "Paid into the vault",
        "Vekro holds your money.",
        "Vekro holds the buyer's money. Dispatch within 48 hours.",
    ),
    "Seller dispatched transaction": (
        "On the way",
        "The seller sent your item.",
        "You sent the item.",
    ),
    "Dispatch confirmed": ("On the way", "The seller sent your item.", "You sent the item."),
    "Delivery arrived": (
        "Arrived at the door",
        "Your delivery code was sent by SMS. Inspect before you use it.",
        "The buyer is inspecting the item.",
    ),
    "Arrival confirmed": (
        "Arrived at the door",
        "Your delivery code was sent by SMS. Inspect before you use it.",
        "The buyer is inspecting the item.",
    ),
    "OTP confirmed": ("Item accepted", "You accepted the item.", "The buyer accepted the item."),
    "Buyer confirmed OTP": (
        "Item accepted",
        "You accepted the item.",
        "The buyer accepted the item.",
    ),
    "OTP withheld": (
        "Rejected at the door",
        "The item goes back and you're refunded.",
        "The buyer rejected the item at the door.",
    ),
    "Buyer withheld OTP": (
        "Rejected at the door",
        "The item goes back and you're refunded.",
        "The buyer rejected the item at the door.",
    ),
    "Functional issue reported": (
        "Problem reported",
        "You reported a problem.",
        "The buyer reported a problem.",
    ),
    "Functional issue opened": (
        "Problem reported",
        "You reported a problem.",
        "The buyer reported a problem.",
    ),
    "Buyer marked item sent back": (
        "Sent back",
        "You sent the item back.",
        "The buyer sent the item back.",
    ),
    "Buyer initiated return shipment": (
        "Sent back",
        "You sent the item back.",
        "The buyer sent the item back.",
    ),
    "Seller confirmed return receipt": (
        "Return received",
        "The seller received the item.",
        "You received the returned item.",
    ),
    "Return receipt confirmed": (
        "Return received",
        "The seller received the item.",
        "You received the returned item.",
    ),
    "Seller resolution submitted": (
        "Seller chose a fix",
        "The seller chose how to fix it.",
        "You chose how to fix it.",
    ),
    "Resolution action recorded": (
        "Seller chose a fix",
        "The seller chose how to fix it.",
        "You chose how to fix it.",
    ),
    "Buyer reconfirmation recorded": (
        "Buyer replied",
        "You replied to the fix.",
        "The buyer replied to the fix.",
    ),
    "Buyer reconfirmation update": (
        "Buyer replied",
        "You replied to the fix.",
        "The buyer replied to the fix.",
    ),
    "Resolution confirmation updated": (
        "Resolution confirmed",
        "The fix was confirmed.",
        "The fix was confirmed.",
    ),
    "Dispute auto-canceled": (
        "Report closed",
        "The item wasn't sent back in time, so the report closed.",
        "The buyer didn't send the item back in time.",
    ),
    "Dispute auto-escalated": (
        "With Vekro support",
        "Vekro support is reviewing this.",
        "Vekro support is reviewing this.",
    ),
}

_BY_KIND: dict[str, tuple[str, str]] = {
    NotificationEventType.SYSTEM_TIMEOUT.value: (
        "Deadline passed",
        "Vekro acted automatically when time ran out.",
    ),
    NotificationEventType.ADMIN_DECISION.value: (
        "Vekro support decided",
        "Vekro support recorded a decision.",
    ),
    NotificationEventType.DISPUTE_UPDATED.value: (
        "Return updated",
        "The return moved to its next step.",
    ),
    NotificationEventType.DISPUTE_OPENED.value: ("Problem reported", "A problem was reported."),
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _event(notification: Notification, role: UserRole) -> TimelineEvent:
    kind = notification.event_type.value
    known = _BY_TITLE.get(notification.title)
    if known:
        title, buyer_text, seller_text = known
        description = seller_text if role == UserRole.SELLER else buyer_text
    else:
        title, description = _BY_KIND.get(
            kind, ("Order updated", "Your order moved to its next step.")
        )
    if kind == NotificationEventType.TRANSACTION_CREATED.value:
        kind = TRANSACTION_CREATED
    return TimelineEvent(
        kind=kind, at=_utc(notification.created_at), title=title, description=description
    )


def get_transaction_timeline(
    db: Session,
    *,
    transaction_id: uuid.UUID,
    viewer: User,
) -> list[TimelineEvent]:
    """Raises TransactionNotVisibleError for anyone but the buyer, the seller or an admin."""
    view = get_transaction_for_viewer(db, transaction_id=transaction_id, viewer=viewer)
    query = select(Notification).where(Notification.transaction_id == transaction_id)
    # Admins read the buyer's side, so each event appears once.
    owner = view.transaction.buyer_id if view.viewer_role == UserRole.ADMIN else viewer.id
    query = query.where(Notification.user_id == owner).order_by(Notification.created_at.asc())
    role = UserRole.BUYER if view.viewer_role == UserRole.ADMIN else view.viewer_role
    return [_event(item, role) for item in db.execute(query).scalars().all()]
