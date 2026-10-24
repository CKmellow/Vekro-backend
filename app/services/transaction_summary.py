"""Money totals for the seller dashboard."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole

EAT = ZoneInfo("Africa/Nairobi")

IN_ESCROW = frozenset(
    {
        TransactionStatus.LOCKED,
        TransactionStatus.OUT_FOR_DELIVERY,
        TransactionStatus.DISPATCHED,
        TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        TransactionStatus.HOLD_24H,
        TransactionStatus.DISPUTED_FUNCTIONAL,
        TransactionStatus.RETURN_IN_TRANSIT,
        TransactionStatus.RETURN_RECEIVED,
        TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
        TransactionStatus.ESCALATED_ADMIN_REVIEW,
    }
)
ISSUES = frozenset(
    {
        TransactionStatus.DISPUTED_FUNCTIONAL,
        TransactionStatus.RETURN_IN_TRANSIT,
        TransactionStatus.RETURN_RECEIVED,
        TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
        TransactionStatus.ESCALATED_ADMIN_REVIEW,
    }
)
RELEASED = frozenset({TransactionStatus.RELEASED, TransactionStatus.RESOLVED_RELEASE})


class SummaryForbiddenError(Exception):
    """Only sellers have a dashboard."""


@dataclass(frozen=True)
class Total:
    amount: Decimal
    orders: int


@dataclass(frozen=True)
class SellerSummary:
    in_escrow: Total
    released_this_month: Total
    released_last_month: Total
    awaiting_dispatch: int
    open_issues: int
    next_release: tuple[uuid.UUID, str] | None


def _month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _eat(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(EAT)


def seller_summary(db: Session, *, seller: User, now: datetime | None = None) -> SellerSummary:
    if seller.role != UserRole.SELLER:
        raise SummaryForbiddenError("Only sellers have a dashboard summary.")
    this_month = _month_start(_eat(now or datetime.now(UTC)))
    last_month = _month_start(this_month - timedelta(days=1))

    rows = db.execute(select(Transaction).where(Transaction.seller_id == seller.id)).scalars().all()

    def total(items: list[Transaction]) -> Total:
        return Total(sum((t.amount for t in items), Decimal("0.00")), len(items))

    escrow = [t for t in rows if t.status in IN_ESCROW]
    released = [t for t in rows if t.status in RELEASED]
    this = [t for t in released if _eat(t.updated_at) >= this_month]
    last = [t for t in released if last_month <= _eat(t.updated_at) < this_month]
    held = sorted(
        (t for t in rows if t.status == TransactionStatus.HOLD_24H and t.hold_started_at),
        key=lambda t: _eat(t.hold_started_at),  # type: ignore[arg-type]
    )
    return SellerSummary(
        in_escrow=total(escrow),
        released_this_month=total(this),
        released_last_month=total(last),
        awaiting_dispatch=sum(1 for t in rows if t.status == TransactionStatus.LOCKED),
        open_issues=sum(1 for t in rows if t.status in ISSUES),
        next_release=(held[0].id, held[0].reference) if held else None,
    )
