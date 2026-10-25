"""Seller analytics (frontend gap #148): the seller's own orders over 7, 30 or 90 days, in EAT.

Definitions
- Orders: placed in the period and paid (not initiated or awaiting payment).
- Sales volume: amounts released to the seller in the period (released or resolved_release).
- Completion rate: released orders out of closed orders, for orders placed in the period.
- Dispute rate: orders placed in the period that had an issue reported.
- Time to dispatch: payment (locked_at) to dispatch, for orders dispatched in the period.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute
from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction, TransactionStatus
from app.services.listing_content import UNPAID_STATUSES
from app.services.transaction_list import SEGMENT_STATUSES, OrderSegment

EAT = ZoneInfo("Africa/Nairobi")
RELEASED = {TransactionStatus.RELEASED, TransactionStatus.RESOLVED_RELEASE}
REFUNDED = {
    TransactionStatus.REFUNDED_BUYER,
    TransactionStatus.RESOLVED_REFUND,
    TransactionStatus.RETURNED_TO_SELLER,
}
CLOSED = SEGMENT_STATUSES[OrderSegment.COMPLETED]
RANGES = {"7d": 7, "30d": 30, "90d": 90}


@dataclass
class Kpis:
    sales_volume: Decimal = Decimal("0")
    orders: int = 0
    average_order_value: Decimal | None = None
    completion_rate: float | None = None
    dispute_rate: float | None = None
    average_dispatch_seconds: int | None = None


@dataclass
class TopListing:
    listing_id: uuid.UUID
    title: str
    photo_url: str | None
    orders: int
    sales: Decimal
    completion_rate: float | None


@dataclass
class Analytics:
    range_days: int
    completed_sales: int
    current: Kpis
    previous: Kpis
    sales_series: list[tuple[date, Decimal, int]] = field(default_factory=list)
    bucket: str = "day"
    outcomes: list[tuple[date, int, int, int]] = field(default_factory=list)
    dispute_categories: list[tuple[str, int]] = field(default_factory=list)
    top_listings: list[TopListing] = field(default_factory=list)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _day(value: datetime) -> date:
    return _aware(value).astimezone(EAT).date()  # type: ignore[union-attr]


def _week(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _kpis(
    placed: list[Transaction],
    released: list[Transaction],
    dispatched: list[Transaction],
    disputed: set[uuid.UUID],
) -> Kpis:
    k = Kpis()
    k.orders = len(placed)
    k.sales_volume = sum((t.amount for t in released), Decimal("0"))
    if placed:
        k.average_order_value = (
            sum((t.amount for t in placed), Decimal("0")) / len(placed)
        ).quantize(Decimal("1"))
        k.dispute_rate = round(sum(1 for t in placed if t.id in disputed) / len(placed), 4)
    closed = [t for t in placed if t.status in CLOSED]
    if closed:
        k.completion_rate = round(sum(1 for t in closed if t.status in RELEASED) / len(closed), 4)
    waits = [
        (_aware(t.dispatched_at) - _aware(t.locked_at)).total_seconds()  # type: ignore[operator]
        for t in dispatched
        if t.locked_at is not None and t.dispatched_at is not None
    ]
    if waits:
        k.average_dispatch_seconds = int(sum(waits) / len(waits))
    return k


def seller_analytics(
    db: Session, seller_id: uuid.UUID, range_key: str, now: datetime | None = None
) -> Analytics:
    days = RANGES[range_key]
    current_time = _aware(now) or datetime.now(UTC)
    today = current_time.astimezone(EAT).date()
    start_day = today - timedelta(days=days - 1)
    prev_start_day = start_day - timedelta(days=days)

    def window(t: datetime | None, lo: date, hi: date) -> bool:
        return t is not None and lo <= _day(t) <= hi

    rows = list(
        db.execute(select(Transaction).where(Transaction.seller_id == seller_id)).scalars().all()
    )
    paid = [t for t in rows if t.status not in UNPAID_STATUSES]
    ids = [t.id for t in paid]
    disputes = (
        list(db.execute(select(Dispute).where(Dispute.transaction_id.in_(ids))).scalars().all())
        if ids
        else []
    )
    disputed = {d.transaction_id for d in disputes}

    def period(lo: date, hi: date) -> Kpis:
        return _kpis(
            [t for t in paid if window(t.created_at, lo, hi)],
            [t for t in paid if t.status in RELEASED and window(t.released_at, lo, hi)],
            [t for t in paid if window(t.dispatched_at, lo, hi)],
            disputed,
        )

    result = Analytics(
        range_days=days,
        completed_sales=sum(1 for t in paid if t.status in RELEASED),
        current=period(start_day, today),
        previous=period(prev_start_day, start_day - timedelta(days=1)),
    )

    # Sales over time: daily, weekly at 90 days.
    weekly = days >= 90
    result.bucket = "week" if weekly else "day"
    buckets: dict[date, list] = {}
    cursor = _week(start_day) if weekly else start_day
    while cursor <= today:
        buckets[cursor] = [Decimal("0"), 0]
        cursor += timedelta(days=7 if weekly else 1)
    for t in paid:
        if t.status in RELEASED and window(t.released_at, start_day, today):
            d = _day(t.released_at)  # type: ignore[arg-type]
            key = _week(d) if weekly else d
            if key in buckets:
                buckets[key][0] += t.amount
                buckets[key][1] += 1
    result.sales_series = [(k, v[0], v[1]) for k, v in sorted(buckets.items())]

    # Outcomes per week, by when the order was placed.
    placed_now = [t for t in paid if window(t.created_at, start_day, today)]
    weeks: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0])
    cursor = _week(start_day)
    while cursor <= today:
        weeks[cursor]
        cursor += timedelta(days=7)
    for t in placed_now:
        w = weeks[_week(_day(t.created_at))]
        if t.id in disputed:
            w[2] += 1
        elif t.status in RELEASED:
            w[0] += 1
        elif t.status in REFUNDED:
            w[1] += 1
    result.outcomes = [(k, *v) for k, v in sorted(weeks.items())]  # type: ignore[misc]

    placed_ids = {t.id for t in placed_now}
    categories = Counter(d.reason for d in disputes if d.transaction_id in placed_ids)
    result.dispute_categories = categories.most_common()

    # Top listings by orders in the period.
    by_listing: dict[uuid.UUID, list[Transaction]] = defaultdict(list)
    for t in placed_now:
        by_listing[t.listing_id].append(t)
    top = sorted(by_listing.items(), key=lambda kv: (-len(kv[1]), -sum(t.amount for t in kv[1])))[
        :5
    ]
    listing_ids = [lid for lid, _ in top]
    listings = (
        {
            listing.id: listing
            for listing in db.execute(select(Listing).where(Listing.id.in_(listing_ids))).scalars()
        }
        if listing_ids
        else {}
    )
    covers = (
        {
            p.listing_id: p.id
            for p in db.execute(
                select(ListingPhoto).where(
                    ListingPhoto.listing_id.in_(listing_ids), ListingPhoto.position == 0
                )
            ).scalars()
        }
        if listing_ids
        else {}
    )
    for lid, items in top:
        closed = [t for t in items if t.status in CLOSED]
        listing = listings.get(lid)
        result.top_listings.append(
            TopListing(
                listing_id=lid,
                title=listing.title if listing else "Listing",
                photo_url=f"/listings/{lid}/photos/{covers[lid]}" if lid in covers else None,
                orders=len(items),
                sales=sum((t.amount for t in items if t.status in RELEASED), Decimal("0")),
                completion_rate=(
                    round(sum(1 for t in closed if t.status in RELEASED) / len(closed), 4)
                    if closed
                    else None
                ),
            )
        )
    return result
