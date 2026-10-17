import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Escrow(Base):
    __tablename__ = "escrows"
    __table_args__ = (
        CheckConstraint("amount_total > 0", name="ck_escrows_amount_total_positive"),
        CheckConstraint("funded_amount >= 0", name="ck_escrows_funded_amount_non_negative"),
        CheckConstraint("released_amount >= 0", name="ck_escrows_released_amount_non_negative"),
        CheckConstraint("refunded_amount >= 0", name="ck_escrows_refunded_amount_non_negative"),
        CheckConstraint("funded_amount <= amount_total", name="ck_escrows_funded_le_total"),
        CheckConstraint(
            "released_amount + refunded_amount <= funded_amount",
            name="ck_escrows_outflows_le_funded",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    seller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    amount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    funded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
    )
    released_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
    )
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="KES",
        server_default=text("'KES'"),
    )

    # M20: which custody provider holds this escrow. Sticky once set at open
    # (routing in E5); defaults to 'simulated' so existing rows keep working.
    custody_provider: Mapped[str] = mapped_column(
        String(40),
        nullable=True,
        default="simulated",
        server_default=text("'simulated'"),
    )
    # eConfirm confirmation_code, Fernet-encrypted at rest (A3). Never stored or
    # logged in clear. Populated at open time in E5; nullable for simulated rows.
    econfirm_confirmation_code: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The payer M-Pesa number used to fund an eConfirm escrow; reversal returns
    # funds there (A5). Recorded for traceability; populated at funding in E5.
    econfirm_payer_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

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
