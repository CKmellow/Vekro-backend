"""A member's store (M28, docs/accounts-and-modes.md): selling is a capability of the account.

A store opens, closes and reopens; it is never deleted. Each change is a ``store_events`` row,
anchored as ``store_opened`` or ``store_closed`` in the global chain (seller UUID and time only),
so a seller's public track record shows when the store was open. Closing waits until no order
still needs the seller: funded orders not yet final, money still held, payouts not yet settled,
open disputes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dispute import Dispute, DisputeStatus
from app.models.escrow import Escrow
from app.models.listing import Listing
from app.models.store_event import STORE_CLOSED, STORE_OPENED, StoreEvent
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus
from app.models.user import SELLER_TERMS_VERSION, StoreStatus, User, UserRole
from app.services.anchoring.recorder import record_store_event

# Orders that no longer need the seller. Unpaid orders don't block a close either: they hold no
# money, and a closed store's unpaid orders can't be paid any more.
FINAL_STATUSES = frozenset(
    {
        TransactionStatus.RELEASED,
        TransactionStatus.REFUNDED_BUYER,
        TransactionStatus.RESOLVED_RELEASE,
        TransactionStatus.RESOLVED_REFUND,
        TransactionStatus.RESOLVED_SPLIT,
    }
)
UNPAID_STATUSES = frozenset({TransactionStatus.INITIATED, TransactionStatus.AWAITING_PAYMENT})
OPEN_DISPUTES = frozenset(
    {
        DisputeStatus.OPEN,
        DisputeStatus.BUYER_SENT_BACK,
        DisputeStatus.RETURN_RECEIVED,
        DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
        DisputeStatus.ESCALATED_ADMIN_REVIEW,
    }
)
UNSETTLED_PAYOUTS = (TransactionPayoutStatus.PENDING, TransactionPayoutStatus.UNKNOWN)

CLOSED_MESSAGE = "This store is closed."


class StoreNotAllowedError(Exception):
    """Admins never hold a store, or the store is not in a state that allows this."""


@dataclass
class CloseBlockers:
    open_orders: list[str] = field(default_factory=list)
    held_amount: Decimal = Decimal("0.00")
    unsettled_payouts: list[str] = field(default_factory=list)
    open_disputes: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(
            self.open_orders or self.held_amount > 0 or self.unsettled_payouts or self.open_disputes
        )


class StoreCloseBlockedError(Exception):
    def __init__(self, blockers: CloseBlockers):
        super().__init__("This store still has orders to finish.")
        self.blockers = blockers


def store_closed_for(db: Session, transaction: Transaction) -> bool:
    """A closed store's unpaid orders can't be paid any more (no new money into a closed store)."""
    seller = db.get(User, transaction.seller_id)
    return seller is None or seller.store_status == StoreStatus.CLOSED


def open_store_on(
    user: User, *, now: datetime | None = None, terms_version: str = SELLER_TERMS_VERSION
) -> User:
    """Mark the account's store open with the accepted seller terms. Callers that persist the user
    also call ``record_store_opened`` (after a flush) so the opening is on the record."""
    if user.role == UserRole.ADMIN:
        raise StoreNotAllowedError("Admin accounts can't open a store.")
    current = now or datetime.now(UTC)
    user.store_status = StoreStatus.ACTIVE
    user.store_opened_at = current
    user.store_closed_at = None
    user.seller_terms_version = terms_version
    user.seller_terms_accepted_at = current
    return user


def _record(
    db: Session,
    user: User,
    kind: str,
    at: datetime,
    *,
    reopened: bool = False,
    migrated: bool = False,
) -> StoreEvent:
    if user.id is None:
        user.id = uuid.uuid4()
    # A new account (register, Google sign-up, demo seed) must be written before its store event:
    # store_events.user_id is a foreign key, and the flush doesn't order unrelated mappers by it.
    db.add(user)
    db.flush()
    event = StoreEvent(
        id=uuid.uuid4(),
        user_id=user.id,
        kind=kind,
        reopened=reopened,
        migrated=migrated,
        at=at,
        created_at=datetime.now(UTC),
    )
    record_store_event(db, event)
    db.add(event)
    return event


def record_store_opened(db: Session, user: User, *, reopened: bool = False) -> StoreEvent:
    return _record(
        db, user, STORE_OPENED, user.store_opened_at or datetime.now(UTC), reopened=reopened
    )


def start_selling(
    db: Session,
    user: User,
    *,
    shop_name: str,
    shop_area: str | None,
    terms_version: str,
    now: datetime | None = None,
) -> User:
    """ "Start selling" from the buyer area: name the store and accept the seller terms. The store
    opens at once but takes orders only after its payout number is verified by SMS."""
    if user.role == UserRole.ADMIN:
        raise StoreNotAllowedError("Admin accounts can't open a store.")
    if user.store_status == StoreStatus.ACTIVE:
        raise StoreNotAllowedError("Your store is already open.")
    if user.store_status == StoreStatus.CLOSED:
        raise StoreNotAllowedError("Your store is closed. Reopen it instead.")
    if terms_version != SELLER_TERMS_VERSION:
        raise StoreNotAllowedError("Accept the current seller terms to open a store.")
    open_store_on(user, now=now, terms_version=terms_version)
    user.shop_name = shop_name.strip()
    if shop_area is not None:
        user.shop_area = shop_area.strip() or None
    record_store_opened(db, user)
    db.commit()
    db.refresh(user)
    return user


def close_blockers(db: Session, user: User) -> CloseBlockers:
    blockers = CloseBlockers()
    orders = db.execute(select(Transaction).where(Transaction.seller_id == user.id)).scalars().all()
    for order in orders:
        if order.status not in FINAL_STATUSES and order.status not in UNPAID_STATUSES:
            blockers.open_orders.append(order.reference)
        if order.payout_status in UNSETTLED_PAYOUTS:
            blockers.unsettled_payouts.append(order.reference)
    held = db.execute(
        select(
            func.coalesce(
                func.sum(Escrow.funded_amount - Escrow.released_amount - Escrow.refunded_amount),
                0,
            )
        ).where(Escrow.seller_id == user.id)
    ).scalar_one()
    blockers.held_amount = Decimal(held or 0).quantize(Decimal("0.01"))
    disputed = db.execute(
        select(Transaction.reference)
        .join(Dispute, Dispute.transaction_id == Transaction.id)
        .where(Transaction.seller_id == user.id, Dispute.status.in_(OPEN_DISPUTES))
    ).scalars()
    blockers.open_disputes = sorted(set(disputed))
    blockers.open_orders.sort()
    blockers.unsettled_payouts.sort()
    return blockers


def close_store(db: Session, user: User, *, now: datetime | None = None) -> User:
    """Archive every listing, stop new orders and keep all history. Refused while anything still
    needs the seller (StoreCloseBlockedError lists it)."""
    if user.store_status != StoreStatus.ACTIVE:
        raise StoreNotAllowedError("Only an open store can be closed.")
    blockers = close_blockers(db, user)
    if blockers.any:
        raise StoreCloseBlockedError(blockers)
    current = now or datetime.now(UTC)
    for listing in db.execute(
        select(Listing).where(Listing.seller_id == user.id, Listing.archived_at.is_(None))
    ).scalars():
        listing.archived_at = current
    user.store_status = StoreStatus.CLOSED
    user.store_closed_at = current
    _record(db, user, STORE_CLOSED, current)
    db.commit()
    db.refresh(user)
    return user


def reopen_store(db: Session, user: User, *, now: datetime | None = None) -> User:
    """Open a closed store again. Its listings stay archived until the seller restores them."""
    if user.store_status != StoreStatus.CLOSED:
        raise StoreNotAllowedError("Only a closed store can be reopened.")
    current = now or datetime.now(UTC)
    user.store_status = StoreStatus.ACTIVE
    user.store_opened_at = current
    user.store_closed_at = None
    record_store_opened(db, user, reopened=True)
    db.commit()
    db.refresh(user)
    return user


def backfill_store_events(db: Session) -> int:
    """Record ``store_opened`` (and ``store_closed``) for stores from before M28. Idempotent."""
    recorded = 0
    with_events = set(db.execute(select(StoreEvent.user_id).distinct()).scalars())
    for user in db.execute(
        select(User).where(User.store_status != StoreStatus.NONE, User.role != UserRole.ADMIN)
    ).scalars():
        if user.id in with_events:
            continue
        opened = user.store_opened_at or user.created_at or datetime.now(UTC)
        _record(db, user, STORE_OPENED, opened, migrated=True)
        recorded += 1
        if user.store_status == StoreStatus.CLOSED:
            _record(db, user, STORE_CLOSED, user.store_closed_at or opened, migrated=True)
    db.commit()
    return recorded
