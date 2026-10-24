"""The caller's own orders, filtered and paged for the buyer and seller order lists."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole


class OrderListForbiddenError(Exception):
    """Only buyers and sellers have their own orders to list."""


class OrderSegment(StrEnum):
    NEEDS_ACTION = "needs_action"
    IN_TRANSIT = "in_transit"
    HELD = "held"
    COMPLETED = "completed"
    ISSUES = "issues"


# The server owns this grouping so list filters and counts always agree.
SEGMENT_STATUSES: dict[OrderSegment, frozenset[TransactionStatus]] = {
    OrderSegment.NEEDS_ACTION: frozenset(
        {
            TransactionStatus.LOCKED,
            TransactionStatus.OUT_FOR_DELIVERY,
            TransactionStatus.RETURN_IN_TRANSIT,
            TransactionStatus.RETURN_RECEIVED,
        }
    ),
    OrderSegment.IN_TRANSIT: frozenset(
        {
            TransactionStatus.INITIATED,
            TransactionStatus.AWAITING_PAYMENT,
            TransactionStatus.DISPATCHED,
            TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        }
    ),
    OrderSegment.HELD: frozenset({TransactionStatus.HOLD_24H}),
    OrderSegment.COMPLETED: frozenset(
        {
            TransactionStatus.RELEASED,
            TransactionStatus.RESOLVED_RELEASE,
            TransactionStatus.REFUNDED_BUYER,
            TransactionStatus.RESOLVED_REFUND,
            TransactionStatus.RETURNED_TO_SELLER,
            TransactionStatus.RESOLVED_SPLIT,
        }
    ),
    OrderSegment.ISSUES: frozenset(
        {
            TransactionStatus.DISPUTED_FUNCTIONAL,
            TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
            TransactionStatus.ESCALATED_ADMIN_REVIEW,
        }
    ),
}


class OrderSort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    AMOUNT_DESC = "amount_desc"
    AMOUNT_ASC = "amount_asc"


@dataclass(frozen=True)
class OrderListItem:
    transaction: Transaction
    listing: Listing
    counterpart: User
    cover_photo_id: uuid.UUID | None


@dataclass(frozen=True)
class OrderPage:
    items: list[OrderListItem]
    total: int
    page: int
    page_size: int
    viewer_role: UserRole
    segment_counts: dict[OrderSegment, int]
    status_counts: dict[TransactionStatus, int]


def _owner_clause(viewer: User):
    if viewer.role == UserRole.BUYER:
        return Transaction.buyer_id == viewer.id
    if viewer.role == UserRole.SELLER:
        return Transaction.seller_id == viewer.id
    raise OrderListForbiddenError("Only buyers and sellers can list their orders.")


def _ordered(query: Select, sort: OrderSort) -> Select:
    if sort == OrderSort.OLDEST:
        return query.order_by(Transaction.created_at.asc(), Transaction.id.asc())
    if sort == OrderSort.AMOUNT_DESC:
        return query.order_by(Transaction.amount.desc(), Transaction.created_at.desc())
    if sort == OrderSort.AMOUNT_ASC:
        return query.order_by(Transaction.amount.asc(), Transaction.created_at.desc())
    return query.order_by(Transaction.created_at.desc(), Transaction.id.desc())


def list_orders_for_viewer(
    db: Session,
    *,
    viewer: User,
    segment: OrderSegment | None = None,
    statuses: list[TransactionStatus] | None = None,
    q: str | None = None,
    sort: OrderSort = OrderSort.NEWEST,
    page: int = 1,
    page_size: int = 20,
) -> OrderPage:
    """One page of the viewer's orders. Counts cover all their orders, before any filter."""
    owner = _owner_clause(viewer)

    status_counts = {
        status: int(count)
        for status, count in db.execute(
            select(Transaction.status, func.count()).where(owner).group_by(Transaction.status)
        ).all()
    }
    segment_counts = {
        segment_key: sum(status_counts.get(s, 0) for s in members)
        for segment_key, members in SEGMENT_STATUSES.items()
    }

    query = select(Transaction, Listing).join(Listing, Listing.id == Transaction.listing_id)
    query = query.where(owner)
    if segment is not None:
        query = query.where(Transaction.status.in_(SEGMENT_STATUSES[segment]))
    if statuses:
        query = query.where(Transaction.status.in_(statuses))
    needle = (q or "").strip().lower()
    if needle:
        query = query.where(
            or_(
                func.lower(Transaction.reference).contains(needle, autoescape=True),
                func.lower(Listing.title).contains(needle, autoescape=True),
            )
        )

    total = int(db.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    rows = db.execute(_ordered(query, sort).offset((page - 1) * page_size).limit(page_size)).all()

    counterpart_ids = {
        row.Transaction.seller_id if viewer.role == UserRole.BUYER else row.Transaction.buyer_id
        for row in rows
    }
    users = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(counterpart_ids))).scalars()}
        if counterpart_ids
        else {}
    )
    covers: dict[uuid.UUID, uuid.UUID] = {}
    listing_ids = {row.Listing.id for row in rows}
    if listing_ids:
        for listing_id, photo_id in db.execute(
            select(ListingPhoto.listing_id, ListingPhoto.id)
            .where(ListingPhoto.listing_id.in_(listing_ids))
            .order_by(ListingPhoto.position.desc())
        ).all():
            covers[listing_id] = photo_id  # lowest position wins (written last)

    items = [
        OrderListItem(
            transaction=row.Transaction,
            listing=row.Listing,
            counterpart=users[
                (
                    row.Transaction.seller_id
                    if viewer.role == UserRole.BUYER
                    else row.Transaction.buyer_id
                )
            ],
            cover_photo_id=covers.get(row.Listing.id),
        )
        for row in rows
    ]
    return OrderPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        viewer_role=viewer.role,
        segment_counts=segment_counts,
        status_counts=status_counts,
    )
