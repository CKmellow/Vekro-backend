import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED_DEFINITE = "failed_definite"
    UNKNOWN = "unknown"


class CollectionAttempt(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_collection_attempts_amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escrow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("escrows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    rail_name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    provider_reference: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="KES",
        server_default=text("'KES'"),
    )
    outcome: Mapped[AttemptOutcome] = mapped_column(
        Enum(
            AttemptOutcome,
            name="attempt_outcome",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        default=AttemptOutcome.UNKNOWN,
        server_default=text("'unknown'"),
        index=True,
    )

    request_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    response_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
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
