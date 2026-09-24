import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.dispute import DisputeStatus, DisputeType


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
