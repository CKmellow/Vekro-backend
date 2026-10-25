import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.models.dispute import AdminDecision, DisputeStatus, SellerResolutionAction
from app.models.transaction import TransactionStatus
from app.services.admin_cases import CaseDetail, CasePage, CaseSummary, CaseTrigger, seller_share_of


class AdminCaseSummaryResponse(BaseModel):
    """One escalated case in the support queue."""

    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    reference: str
    amount: Decimal
    item_title: str
    buyer_name: str
    seller_name: str
    category: str
    trigger: CaseTrigger | None = Field(description="Why the case reached support.")
    escalated_by: str | None = Field(description="buyer or seller, when a party escalated.")
    dispute_status: DisputeStatus
    transaction_status: TransactionStatus
    admin_decision: AdminDecision | None
    opened_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def from_summary(cls, s: CaseSummary) -> "AdminCaseSummaryResponse":
        return cls(
            dispute_id=s.dispute.id,
            transaction_id=s.transaction.id,
            reference=s.transaction.reference,
            amount=s.transaction.amount,
            item_title=s.listing.title,
            buyer_name=s.buyer.name,
            seller_name=s.seller.name,
            category=s.dispute.reason,
            trigger=s.trigger,
            escalated_by=s.escalated_by,
            dispute_status=s.dispute.status,
            transaction_status=s.transaction.status,
            admin_decision=s.dispute.admin_decision,
            opened_at=s.dispute.opened_at,
            escalated_at=s.dispute.escalated_at,
            resolved_at=s.dispute.resolved_at,
        )


class AdminCaseCounters(BaseModel):
    open: int
    older_than_24h: int
    decided_today: int = Field(description="Decided by support today (EAT).")
    average_decision_seconds: int | None = Field(
        description="Average from escalation to decision over the last 30 days."
    )


class AdminCaseListResponse(BaseModel):
    items: list[AdminCaseSummaryResponse]
    total: int
    page: int
    page_size: int
    counters: AdminCaseCounters

    @classmethod
    def from_page(cls, page: CasePage) -> "AdminCaseListResponse":
        return cls(
            items=[AdminCaseSummaryResponse.from_summary(i) for i in page.items],
            total=page.total,
            page=page.page,
            page_size=page.page_size,
            counters=AdminCaseCounters(**page.counters.__dict__),
        )


class AdminCaseParty(BaseModel):
    name: str
    phone: str = Field(description="Full phone number (admins only).")
    orders: int
    completed_sales: int
    dispute_rate: float = Field(description="Share of paid orders that had a dispute (sellers).")


class AdminCaseDecision(BaseModel):
    decision: AdminDecision
    reason: str | None
    seller_share: Decimal | None
    decided_by: str | None
    decided_at: datetime | None


class AdminCaseEvent(BaseModel):
    at: datetime
    title: str
    message: str
    source: str


class AdminCaseDetailResponse(AdminCaseSummaryResponse):
    """Everything support needs to decide a case. Notification payloads are never included."""

    description: str
    unique_id: str | None
    is_serialized: bool
    dispute_policy: dict[str, Any]
    buyer: AdminCaseParty
    seller: AdminCaseParty
    seller_action: SellerResolutionAction | None
    seller_notes: str | None
    buyer_notes: str | None
    reconfirm_rejections: int
    escalation_reason: str | None
    decision: AdminCaseDecision | None
    timeline: list[AdminCaseEvent]

    @classmethod
    def from_detail(cls, d: CaseDetail) -> "AdminCaseDetailResponse":
        s = d.summary
        base = AdminCaseSummaryResponse.from_summary(s).model_dump()
        return cls(
            **base,
            description=s.dispute.description,
            unique_id=s.listing.unique_id,
            is_serialized=s.listing.is_serialized,
            dispute_policy=s.listing.dispute_policy or {},
            buyer=AdminCaseParty(name=s.buyer.name, phone=s.buyer.phone, **d.buyer_stats.__dict__),
            seller=AdminCaseParty(
                name=s.seller.name, phone=s.seller.phone, **d.seller_stats.__dict__
            ),
            seller_action=d.facts.seller_action,
            seller_notes=d.facts.seller_notes,
            buyer_notes=d.facts.buyer_notes,
            reconfirm_rejections=d.facts.reconfirm_rejections,
            escalation_reason=d.escalation_reason,
            decision=(
                AdminCaseDecision(
                    decision=s.dispute.admin_decision,
                    reason=s.dispute.admin_reason,
                    seller_share=seller_share_of(s.dispute),
                    decided_by=d.decided_by,
                    decided_at=s.dispute.resolved_at,
                )
                if s.dispute.admin_decision
                else None
            ),
            timeline=[AdminCaseEvent(**e.__dict__) for e in d.timeline],
        )
