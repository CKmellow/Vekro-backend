import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PhoneCode(Base):
    """A one-time SMS sign-in code. Only its HMAC is stored; a newer code replaces older ones."""

    __tablename__ = "phone_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # What the code confirms: "sign_in" (buyer sign-in) or "payout" (a seller's new payout number).
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sign_in", server_default=text("'sign_in'")
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the code is burned by too many wrong tries: no new code until it passes.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
