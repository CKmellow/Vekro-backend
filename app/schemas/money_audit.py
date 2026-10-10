import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MoneyAuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: datetime = Field(description="When the money-affecting action happened.")
    actor_type: str = Field(description="Who acted: user, admin, system, or provider.")
    actor_id: uuid.UUID | None = Field(
        default=None, description="Acting user id when the actor is a user or admin."
    )
    action: str = Field(description="Action name, for example payout_release_succeeded.")
    reason: str | None = Field(default=None, description="Why the action happened.")
    transaction_id: uuid.UUID | None = Field(default=None, description="Affected transaction.")
    escrow_id: uuid.UUID | None = Field(default=None, description="Affected escrow.")
    attempt_id: uuid.UUID | None = Field(
        default=None, description="Affected collection or payout attempt."
    )
    provider_reference: str | None = Field(
        default=None, description="Provider reference for the rail operation."
    )
    rail_name: str | None = Field(default=None, description="Rail that handled the operation.")
    correlation_id: str = Field(
        description="Correlation key combining transaction_id, provider_reference, and rail."
    )
    amount: Decimal | None = Field(default=None, description="Amount moved or requested.")
    currency: str | None = Field(default=None, description="ISO currency code.")
    details: dict[str, Any] = Field(default_factory=dict, description="Extra context.")


class MoneyAuditEventListResponse(BaseModel):
    items: list[MoneyAuditEventResponse] = Field(
        description="Audit events ordered newest first."
    )
    limit: int
    offset: int
