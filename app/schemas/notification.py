import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.notification import (
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationEventType,
)


class NotificationResponse(BaseModel):
    """Notification entry returned to the authenticated user feed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    transaction_id: uuid.UUID
    transaction_reference: str | None = Field(
        default=None, description="The order's human reference, for example VK-7K3P9Q."
    )
    event_type: NotificationEventType
    channel: NotificationChannel
    title: str
    message: str
    payload: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    delivered_at: datetime | None
    delivery_status: NotificationDeliveryStatus = NotificationDeliveryStatus.NOT_ATTEMPTED
    created_at: datetime
    audience: Literal["buying", "selling"] | None = Field(
        default=None,
        description=(
            "buying: about an order this account paid for; selling: about its store's order. "
            "Each shell lists its own."
        ),
    )

    @field_validator("delivery_status", mode="before")
    @classmethod
    def _default_delivery_status(cls, value: Any) -> Any:
        return NotificationDeliveryStatus.NOT_ATTEMPTED if value is None else value
