"""Read-only order search for the support console."""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.dispute import Dispute
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User

EAT = ZoneInfo("Africa/Nairobi")


@dataclass(frozen=True)
class SearchRow:
    transaction: Transaction
    listing: Listing
    buyer: User
    seller: User
    last_event: tuple[str, datetime] | None
    dispute_id: uuid.UUID | None


@dataclass(frozen=True)
class SearchPage:
    items: list[SearchRow]
    total: int
    page: int
    page_size: int


def _eat_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=EAT).astimezone(UTC)


def search_transactions(
    db: Session,
    *,
    q: str | None = None,
    statuses: list[TransactionStatus] | None = None,
    placed_from: date | None = None,
    placed_to: date | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    serialized: bool | None = None,
    page: int = 1,
    page_size: int = 25,
) -> SearchPage:
    buyer = aliased(User)
    seller = aliased(User)
    query = (
        select(Transaction, Listing, buyer, seller)
        .join(Listing, Listing.id == Transaction.listing_id)
        .join(buyer, buyer.id == Transaction.buyer_id)
        .join(seller, seller.id == Transaction.seller_id)
    )
    needle = (q or "").strip()
    if needle:
        lowered = needle.lower()
        matches = [
            func.lower(Transaction.reference).contains(lowered, autoescape=True),
            func.lower(seller.name).contains(lowered, autoescape=True),
            func.lower(Listing.title).contains(lowered, autoescape=True),
            func.lower(Listing.unique_id).contains(lowered, autoescape=True),
        ]
        digits = re.sub(r"\D", "", needle)
        if len(digits) >= 6:
            # Phones are stored as +254XXXXXXXXX; match the national part however it was typed.
            tail = digits[-9:]
            matches += [buyer.phone.endswith(tail), seller.phone.endswith(tail)]
        query = query.where(or_(*matches))
    if statuses:
        query = query.where(Transaction.status.in_(statuses))
    if placed_from:
        query = query.where(Transaction.created_at >= _eat_start(placed_from))
    if placed_to:
        query = query.where(Transaction.created_at < _eat_start(placed_to + timedelta(days=1)))
    if min_amount is not None:
        query = query.where(Transaction.amount >= min_amount)
    if max_amount is not None:
        query = query.where(Transaction.amount <= max_amount)
    if serialized is not None:
        query = query.where(Listing.is_serialized.is_(serialized))

    total = int(db.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    rows = db.execute(
        query.order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    ids = {row[0].id for row in rows}
    seller_of = {row[0].id: row[0].seller_id for row in rows}
    last_events: dict[uuid.UUID, tuple[str, datetime]] = {}
    disputes: dict[uuid.UUID, uuid.UUID] = {}
    if ids:
        for transaction_id, user_id, title, at in db.execute(
            select(
                Notification.transaction_id,
                Notification.user_id,
                Notification.title,
                Notification.created_at,
            )
            .where(
                Notification.transaction_id.in_(ids),
                Notification.user_id.in_(set(seller_of.values())),
            )
            .order_by(Notification.created_at.asc())
        ).all():
            if user_id == seller_of[transaction_id]:  # the seller's copy of each event
                last_events[transaction_id] = (title, at)  # newest written last
        for transaction_id, dispute_id in db.execute(
            select(Dispute.transaction_id, Dispute.id)
            .where(Dispute.transaction_id.in_(ids))
            .order_by(Dispute.created_at.asc())
        ).all():
            disputes[transaction_id] = dispute_id
    return SearchPage(
        items=[
            SearchRow(
                transaction=t,
                listing=li,
                buyer=b,
                seller=s,
                last_event=last_events.get(t.id),
                dispute_id=disputes.get(t.id),
            )
            for t, li, b, s in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
