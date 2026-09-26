import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.dispute import AdminDecision, DisputeStatus, DisputeType
from app.models.transaction import TransactionStatus


class EscalationQueueItemResponse(BaseModel):
    """Admin queue item for escalated disputes awaiting review."""

    model_config = ConfigDict(from_attributes=True)

    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    dispute_type: DisputeType
    dispute_status: DisputeStatus
    reason: str
    description: str
    opened_at: datetime
    escalated_at: datetime | None
    created_at: datetime


class DisputeTimelineEventResponse(BaseModel):
    """Single timeline event shown in admin dispute timeline views."""

    model_config = ConfigDict(from_attributes=True)

    source: str
    event_type: str
    at: datetime
    title: str | None
    message: str | None
    payload: dict[str, Any]


class DisputeCaseTimelineResponse(BaseModel):
    """Full dispute timeline detail for admin triage and decision-making."""

    model_config = ConfigDict(from_attributes=True)

    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    listing_id: uuid.UUID
    buyer_id: uuid.UUID
    seller_id: uuid.UUID
    opened_by_user_id: uuid.UUID
    transaction_status: TransactionStatus
    amount: Decimal
    dispute_type: DisputeType
    dispute_status: DisputeStatus
    reason: str
    description: str
    opened_at: datetime
    escalated_at: datetime | None
    resolved_at: datetime | None
    timeline_events: list[DisputeTimelineEventResponse]


class AdminForceResolveRequest(BaseModel):
    """Admin terminal decision payload for escalated disputes."""

    decision: AdminDecision = Field(description="Terminal decision: refund, release, or split.")
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1),
    ] = Field(description="Required rationale for the admin decision.")


class AdminForceResolveResponse(BaseModel):
    """Result returned after an admin force-resolve decision is applied."""

    model_config = ConfigDict(from_attributes=True)

    dispute_id: uuid.UUID
    transaction_id: uuid.UUID
    decision: AdminDecision
    reason: str
    dispute_status: DisputeStatus
    transaction_status: TransactionStatus
    resolved_at: datetime
    released_at: datetime | None
    refunded_at: datetime | None
