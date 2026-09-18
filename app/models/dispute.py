import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DisputeType(StrEnum):
    PHYSICAL = "physical"
    FUNCTIONAL = "functional"


class DisputeStatus(StrEnum):
    OPEN = "open"
    BUYER_SENT_BACK = "buyer_sent_back"
    RETURN_RECEIVED = "return_received"
    AWAITING_BUYER_RECONFIRMATION = "awaiting_buyer_reconfirmation"
    ESCALATED_ADMIN_REVIEW = "escalated_admin_review"
    RESOLVED_RELEASE = "resolved_release"
    RESOLVED_REFUND = "resolved_refund"
    RESOLVED_SPLIT = "resolved_split"
    CANCELED = "canceled"


class SellerResolutionAction(StrEnum):
    REFUND_ISSUED = "refund_issued"
    REPAIR_SHIPPED = "repair_shipped"
    REPLACEMENT_SHIPPED = "replacement_shipped"


class AdminDecision(StrEnum):
    REFUND = "refund"
    RELEASE = "release"
    SPLIT = "split"


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(
            "split_ratio IS NULL OR (split_ratio > 0 AND split_ratio < 1)",
            name="ck_disputes_split_ratio_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    dispute_type: Mapped[DisputeType] = mapped_column(
        Enum(
            DisputeType,
            name="dispute_type",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(
            DisputeStatus,
            name="dispute_status",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        default=DisputeStatus.OPEN,
        server_default=text("'open'"),
        index=True,
    )

    reason: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    seller_resolution_action: Mapped[SellerResolutionAction | None] = mapped_column(
        Enum(
            SellerResolutionAction,
            name="seller_resolution_action",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=True,
    )
    admin_decision: Mapped[AdminDecision | None] = mapped_column(
        Enum(
            AdminDecision,
            name="admin_decision",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=True,
    )
    split_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    evidence: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    seller_resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
