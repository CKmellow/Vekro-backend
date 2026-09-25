import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.notification import NotificationChannel, NotificationEventType


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    transaction_id: uuid.UUID
    event_type: NotificationEventType
    channel: NotificationChannel
    title: str
    message: str
    payload: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
