import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base


class UserRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=True,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    mpesa_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mpesa_account_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Seller shop profile (S3, S13).
    shop_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    shop_area: Mapped[str | None] = mapped_column(String(80), nullable=True)
    shop_about: Mapped[str | None] = mapped_column(String(280), nullable=True)
    logo_content_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logo_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # {"payment_received": {"in_app": true, "sms": false}, ...}; null means everything on.
    notification_prefs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Deferred so user reads never load image bytes.
    logo_data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))

    session_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # M23 USSD: a short PIN for sensitive USSD actions (hashed like passwords), with its own
    # lockout so guessing it over USSD cannot lock the web login.
    ussd_pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Continue with Google (#324): matched on the subject only, never the e-mail (kept for display).
    google_sub: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    google_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    google_linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # False for a Google sign-up seller without a password yet (Forgot password sets one).
    has_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    ussd_pin_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ussd_pin_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
