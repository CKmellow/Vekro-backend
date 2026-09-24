import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models.dispute import AdminDecision, DisputeStatus, DisputeType
from app.models.transaction import TransactionStatus


class EscalationQueueItemResponse(BaseModel):
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
    model_config = ConfigDict(from_attributes=True)

    source: str
    event_type: str
    at: datetime
    title: str | None
    message: str | None
    payload: dict[str, Any]


class DisputeCaseTimelineResponse(BaseModel):
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
    decision: AdminDecision
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AdminForceResolveResponse(BaseModel):
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
