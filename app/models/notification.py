import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NotificationEventType(StrEnum):
    TRANSACTION_CREATED = "transaction_created"
    TRANSACTION_LOCKED = "transaction_locked"
    TRANSACTION_DISPATCHED = "transaction_dispatched"
    DELIVERY_ARRIVED = "delivery_arrived"
    OTP_GIVEN = "otp_given"
    OTP_WITHHELD = "otp_withheld"
    DISPUTE_OPENED = "dispute_opened"
    DISPUTE_UPDATED = "dispute_updated"
    ADMIN_DECISION = "admin_decision"
    SYSTEM_TIMEOUT = "system_timeout"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "(NOT is_read) OR (read_at IS NOT NULL)",
            name="ck_notifications_read_at_when_read",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[NotificationEventType] = mapped_column(
        Enum(
            NotificationEventType,
            name="notification_event_type",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(
            NotificationChannel,
            name="notification_channel",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        default=NotificationChannel.IN_APP,
        server_default=text("'in_app'"),
    )

    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
