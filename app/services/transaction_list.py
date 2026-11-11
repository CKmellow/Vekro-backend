"""The caller's own orders, filtered and paged for the buyer and seller order lists."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute
from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import PartyRole, User, UserRole
from app.services.dispute_summary import latest_categories


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


class BuyerFilter(StrEnum):
    """The buyer area's Orders filters (M28). ``cases`` is Returns & disputes: every order that
    had a dispute or return, open or past."""

    ACTIVE = "active"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    DISPUTED = "disputed"
    CASES = "cases"


BUYER_FILTER_STATUSES: dict[BuyerFilter, frozenset[TransactionStatus]] = {
    BuyerFilter.COMPLETED: frozenset(
        {TransactionStatus.RELEASED, TransactionStatus.RESOLVED_RELEASE}
    ),
    # A split gives part of the money back, so it sits with the refunds.
    BuyerFilter.REFUNDED: frozenset(
        {
            TransactionStatus.REFUNDED_BUYER,
            TransactionStatus.RESOLVED_REFUND,
            TransactionStatus.RESOLVED_SPLIT,
            TransactionStatus.RETURNED_TO_SELLER,
        }
    ),
    BuyerFilter.DISPUTED: frozenset(
        {
            TransactionStatus.DISPUTED_FUNCTIONAL,
            TransactionStatus.RETURN_IN_TRANSIT,
            TransactionStatus.RETURN_RECEIVED,
            TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
            TransactionStatus.ESCALATED_ADMIN_REVIEW,
        }
    ),
}
BUYER_FILTER_STATUSES[BuyerFilter.ACTIVE] = frozenset(TransactionStatus) - (
    BUYER_FILTER_STATUSES[BuyerFilter.COMPLETED]
    | BUYER_FILTER_STATUSES[BuyerFilter.REFUNDED]
    | BUYER_FILTER_STATUSES[BuyerFilter.DISPUTED]
)


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
    dispute_category: str | None = None


@dataclass(frozen=True)
class OrderPage:
    items: list[OrderListItem]
    total: int
    page: int
    page_size: int
    viewer_role: PartyRole
    segment_counts: dict[OrderSegment, int]
    status_counts: dict[TransactionStatus, int]
    filter_counts: dict[BuyerFilter, int] | None = None


class OrderSide(StrEnum):
    BUYING = "buying"
    SELLING = "selling"


SIDE_ROLE = {OrderSide.BUYING: PartyRole.BUYER, OrderSide.SELLING: PartyRole.SELLER}


def resolve_side(viewer: User, side: PartyRole | None) -> PartyRole:
    """Which side of the viewer's orders to list: ``as=buying`` or ``as=selling``.

    Without one, an account with a store sees its sales and any other account its purchases, as
    before M28. Admins have no orders of their own."""
    if viewer.role == UserRole.ADMIN:
        raise OrderListForbiddenError("Only buyers and sellers can list their orders.")
    if side is None:
        return PartyRole.SELLER if viewer.has_store else PartyRole.BUYER
    if side == PartyRole.SELLER and not viewer.has_store:
        raise OrderListForbiddenError("Open a store to see orders as a seller.")
    if side not in (PartyRole.BUYER, PartyRole.SELLER):
        raise OrderListForbiddenError("Only buyers and sellers can list their orders.")
    return side


def _owner_clause(viewer: User, side: PartyRole):
    if side == PartyRole.BUYER:
        return Transaction.buyer_id == viewer.id
    return Transaction.seller_id == viewer.id


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
    side: PartyRole | None = None,
    buyer_filter: BuyerFilter | None = None,
) -> OrderPage:
    """One page of the viewer's orders. Counts cover all their orders, before any filter."""
    side = resolve_side(viewer, side)
    owner = _owner_clause(viewer, side)

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

    with_cases = select(Dispute.transaction_id)
    filter_counts = None
    if side == PartyRole.BUYER:
        filter_counts = {
            key: sum(status_counts.get(s, 0) for s in members)
            for key, members in BUYER_FILTER_STATUSES.items()
        }
        filter_counts[BuyerFilter.CASES] = int(
            db.execute(
                select(func.count())
                .select_from(Transaction)
                .where(owner, Transaction.id.in_(with_cases))
            ).scalar_one()
        )

    query = select(Transaction, Listing).join(Listing, Listing.id == Transaction.listing_id)
    query = query.where(owner)
    if buyer_filter == BuyerFilter.CASES:
        query = query.where(Transaction.id.in_(with_cases))
    elif buyer_filter is not None:
        query = query.where(Transaction.status.in_(BUYER_FILTER_STATUSES[buyer_filter]))
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
        row.Transaction.seller_id if side == PartyRole.BUYER else row.Transaction.buyer_id
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

    categories = latest_categories(db, {row.Transaction.id for row in rows})
    items = [
        OrderListItem(
            transaction=row.Transaction,
            listing=row.Listing,
            counterpart=users[
                (row.Transaction.seller_id if side == PartyRole.BUYER else row.Transaction.buyer_id)
            ],
            cover_photo_id=covers.get(row.Listing.id),
            dispute_category=categories.get(row.Transaction.id),
        )
        for row in rows
    ]
    return OrderPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        viewer_role=side,
        segment_counts=segment_counts,
        status_counts=status_counts,
        filter_counts=filter_counts,
    )
