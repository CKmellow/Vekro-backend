"""Liabilities snapshots (M24 B2, #261): the Merkle sum tree over every open escrow, kept so each
buyer can be served the proof that their held amount is counted in the anchored total."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_BIGINT_ID = BigInteger().with_variant(Integer, "sqlite")


class LiabilitiesSnapshot(Base):
    __tablename__ = "liabilities_snapshots"

    id: Mapped[int] = mapped_column(_BIGINT_ID, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # None when nothing is held: an empty tree has no root.
    root_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    total_cents: Mapped[Decimal] = mapped_column(Numeric(20, 0), nullable=False)
    leaf_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The global liabilities_snapshot event. Not a foreign key: anchor_events is append-only.
    anchor_event_id: Mapped[int | None] = mapped_column(_BIGINT_ID, nullable=True)


class LiabilitiesLeaf(Base):
    __tablename__ = "liabilities_leaves"

    snapshot_id: Mapped[int] = mapped_column(
        _BIGINT_ID, ForeignKey("liabilities_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    leaf_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False, index=True
    )
    salt: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    order_commit: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    amount_cents: Mapped[Decimal] = mapped_column(Numeric(20, 0), nullable=False)
