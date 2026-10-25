"""Escalated return cases as the support console reads them."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dispute import AdminDecision, Dispute, DisputeStatus
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.services.dispute_summary import DisputeSummary, summarize

EAT = ZoneInfo("Africa/Nairobi")
DECIDED = frozenset(
    {DisputeStatus.RESOLVED_REFUND, DisputeStatus.RESOLVED_RELEASE, DisputeStatus.RESOLVED_SPLIT}
)
PAID = frozenset(
    set(TransactionStatus) - {TransactionStatus.INITIATED, TransactionStatus.AWAITING_PAYMENT}
)


class AdminCaseNotFoundError(Exception):
    pass


class CaseStatusFilter(StrEnum):
    OPEN = "open"
    DECIDED = "decided"
    ALL = "all"


class CaseTrigger(StrEnum):
    RECEIPT_TIMEOUT = "receipt_timeout"
    BUYER_NOT_FIXED_TWICE = "buyer_not_fixed_twice"
    PARTY_ESCALATED = "party_escalated"
    OTHER_CATEGORY = "other_category"


@dataclass(frozen=True)
class CaseSummary:
    dispute: Dispute
    transaction: Transaction
    listing: Listing
    buyer: User
    seller: User
    trigger: CaseTrigger | None
    escalated_by: str | None


@dataclass(frozen=True)
class CaseCounters:
    open: int
    older_than_24h: int
    decided_today: int
    average_decision_seconds: int | None


@dataclass(frozen=True)
class CasePage:
    items: list[CaseSummary]
    total: int
    page: int
    page_size: int
    counters: CaseCounters


@dataclass(frozen=True)
class PartyStats:
    orders: int
    completed_sales: int
    dispute_rate: float


@dataclass(frozen=True)
class CaseEvent:
    at: datetime
    title: str
    message: str
    source: str


@dataclass(frozen=True)
class CaseDetail:
    summary: CaseSummary
    facts: DisputeSummary
    buyer_stats: PartyStats
    seller_stats: PartyStats
    escalation_reason: str | None
    decided_by: str | None
    timeline: list[CaseEvent]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def case_trigger(dispute: Dispute) -> tuple[CaseTrigger | None, str | None]:
    """Why the case reached support, from what the server recorded."""
    if dispute.escalated_at is None:
        return None, None
    evidence: dict[str, Any] = dispute.evidence or {}
    escalation = evidence.get("escalation")
    if isinstance(escalation, dict) and escalation.get("by") in {"buyer", "seller"}:
        return CaseTrigger.PARTY_ESCALATED, str(escalation["by"])
    if dispute.reason == "other":
        return CaseTrigger.OTHER_CATEGORY, None
    if int(evidence.get("buyer_reconfirm_rejection_count", 0) or 0) >= 2:
        return CaseTrigger.BUYER_NOT_FIXED_TWICE, None
    return CaseTrigger.RECEIPT_TIMEOUT, None


def _summaries(db: Session, rows: list[tuple[Dispute, Transaction]]) -> list[CaseSummary]:
    listing_ids = {t.listing_id for _, t in rows}
    user_ids = {t.buyer_id for _, t in rows} | {t.seller_id for _, t in rows}
    listings = (
        {
            li.id: li
            for li in db.execute(select(Listing).where(Listing.id.in_(listing_ids))).scalars()
        }
        if listing_ids
        else {}
    )
    users = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()}
        if user_ids
        else {}
    )
    out = []
    for dispute, transaction in rows:
        trigger, by = case_trigger(dispute)
        out.append(
            CaseSummary(
                dispute=dispute,
                transaction=transaction,
                listing=listings[transaction.listing_id],
                buyer=users[transaction.buyer_id],
                seller=users[transaction.seller_id],
                trigger=trigger,
                escalated_by=by,
            )
        )
    return out


def case_counters(db: Session, now: datetime | None = None) -> CaseCounters:
    now = now or datetime.now(UTC)
    open_rows = list(
        db.execute(
            select(Dispute.escalated_at).where(
                Dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
            )
        ).scalars()
    )
    older = sum(1 for at in open_rows if at is not None and _utc(at) <= now - timedelta(hours=24))
    decided = list(
        db.execute(
            select(Dispute.escalated_at, Dispute.resolved_at).where(
                Dispute.admin_decision.is_not(None), Dispute.resolved_at.is_not(None)
            )
        ).all()
    )
    today = now.astimezone(EAT).date()
    decided_today = sum(
        1 for _, resolved in decided if _utc(resolved).astimezone(EAT).date() == today
    )
    recent = [
        (_utc(resolved) - _utc(escalated)).total_seconds()
        for escalated, resolved in decided
        if escalated is not None and _utc(resolved) >= now - timedelta(days=30)
    ]
    return CaseCounters(
        open=len(open_rows),
        older_than_24h=older,
        decided_today=decided_today,
        average_decision_seconds=int(sum(recent) / len(recent)) if recent else None,
    )


def list_admin_cases(
    db: Session,
    *,
    status: CaseStatusFilter = CaseStatusFilter.OPEN,
    page: int = 1,
    page_size: int = 25,
    now: datetime | None = None,
) -> CasePage:
    query = select(Dispute, Transaction).join(Transaction, Transaction.id == Dispute.transaction_id)
    if status == CaseStatusFilter.OPEN:
        query = query.where(Dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW).order_by(
            Dispute.escalated_at.asc().nullslast(), Dispute.created_at.asc()
        )
    elif status == CaseStatusFilter.DECIDED:
        query = query.where(Dispute.admin_decision.is_not(None)).order_by(
            Dispute.resolved_at.desc().nullslast()
        )
    else:
        query = query.where(Dispute.escalated_at.is_not(None)).order_by(Dispute.escalated_at.desc())
    total = int(db.execute(select(func.count()).select_from(query.subquery())).scalar_one())
    rows = [
        (d, t) for d, t in db.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    ]
    return CasePage(
        items=_summaries(db, rows),
        total=total,
        page=page,
        page_size=page_size,
        counters=case_counters(db, now),
    )


def _buyer_stats(db: Session, buyer_id: uuid.UUID) -> PartyStats:
    orders = int(
        db.execute(
            select(func.count()).select_from(Transaction).where(Transaction.buyer_id == buyer_id)
        ).scalar_one()
    )
    return PartyStats(orders=orders, completed_sales=0, dispute_rate=0.0)


def _seller_stats(db: Session, seller_id: uuid.UUID) -> PartyStats:
    statuses = list(
        db.execute(select(Transaction.status).where(Transaction.seller_id == seller_id)).scalars()
    )
    paid = [s for s in statuses if s in PAID]
    completed = sum(
        1 for s in statuses if s in {TransactionStatus.RELEASED, TransactionStatus.RESOLVED_RELEASE}
    )
    disputed = int(
        db.execute(
            select(func.count(func.distinct(Dispute.transaction_id)))
            .join(Transaction, Transaction.id == Dispute.transaction_id)
            .where(Transaction.seller_id == seller_id)
        ).scalar_one()
    )
    return PartyStats(
        orders=len(statuses),
        completed_sales=completed,
        dispute_rate=round(disputed / len(paid), 4) if paid else 0.0,
    )


def get_admin_case(db: Session, dispute_id: uuid.UUID) -> CaseDetail:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise AdminCaseNotFoundError("Case not found.")
    transaction = db.get(Transaction, dispute.transaction_id)
    if transaction is None:  # pragma: no cover - FK guarantees
        raise AdminCaseNotFoundError("Case not found.")
    summary = _summaries(db, [(dispute, transaction)])[0]
    evidence = dispute.evidence or {}
    escalation = (
        evidence.get("escalation") if isinstance(evidence.get("escalation"), dict) else None
    )
    decider = db.get(User, dispute.decided_by_user_id) if dispute.decided_by_user_id else None

    # The seller's copies cover every event once. Their messages speak to the seller, so support
    # sees titles only; the report and the escalation come from the dispute, in the parties' words.
    events = [
        CaseEvent(at=n.created_at, title=n.title, message="", source="notification")
        for n in db.execute(
            select(Notification)
            .where(
                Notification.transaction_id == transaction.id,
                Notification.user_id == transaction.seller_id,
            )
            .order_by(Notification.created_at.asc())
        ).scalars()
        if n.event_type != NotificationEventType.DISPUTE_OPENED
        and n.title != "Sent to Vekro support"
    ]
    events.append(CaseEvent(dispute.opened_at, "Problem reported", dispute.description, "dispute"))
    if dispute.escalated_at:
        events.append(
            CaseEvent(
                dispute.escalated_at,
                "Sent to Vekro support",
                (escalation or {}).get("reason") or "The case was escalated for review.",
                "dispute",
            )
        )
    if dispute.resolved_at and dispute.admin_decision:
        events.append(
            CaseEvent(dispute.resolved_at, "Support decided", dispute.admin_reason or "", "support")
        )
    events.sort(key=lambda e: _utc(e.at))

    return CaseDetail(
        summary=summary,
        facts=summarize(dispute),
        buyer_stats=_buyer_stats(db, transaction.buyer_id),
        seller_stats=_seller_stats(db, transaction.seller_id),
        escalation_reason=(escalation or {}).get("reason"),
        decided_by=decider.name if decider else None,
        timeline=events,
    )


def seller_share_of(dispute: Dispute) -> Decimal | None:
    return dispute.split_ratio if dispute.admin_decision == AdminDecision.SPLIT else None
