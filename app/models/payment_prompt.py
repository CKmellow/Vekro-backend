import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaymentPromptKind(StrEnum):
    STK_PROMPT = "stk_prompt"
    REDIRECT = "redirect"


class PaymentPromptStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_TO_INITIATE = "failed_to_initiate"


class PaymentFailureReason(StrEnum):
    CANCELLED = "cancelled"
    WRONG_PIN = "wrong_pin"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"


class PaymentPrompt(Base):
    """One request for the buyer to pay: an STK prompt or a hosted-checkout start."""

    __tablename__ = "payment_prompts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Plain strings (validated by the enums above) so new values need no ALTER TYPE.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    redirect_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    checkout_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
