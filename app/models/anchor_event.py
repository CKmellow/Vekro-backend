"""Append-only log of money-relevant facts, hashed into per-transaction chains and batched into
Merkle roots (M21, docs/anchoring-design.md section 5).

`id` is insert order and may have gaps. `seq` is the gapless global order the batcher assigns
once, together with `batch_id` and `leaf_index` (decision D1). A row is never updated otherwise
and never deleted (see app/db/immutability.py).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.immutability import install_guards

# SQLite only auto-increments an INTEGER PRIMARY KEY.
_BIGINT_ID = BigInteger().with_variant(Integer, "sqlite")


class AnchorEvent(Base):
    __tablename__ = "anchor_events"
    __table_args__ = (
        UniqueConstraint("source_table", "source_id", name="uq_anchor_events_source"),
        UniqueConstraint("seq", name="uq_anchor_events_seq"),
        # Backstops for the hash chains: no two events may claim the same predecessor.
        Index(
            "ux_anchor_events_txn_prev",
            "transaction_id",
            "prev_event_hash",
            unique=True,
            postgresql_where=text("transaction_id IS NOT NULL"),
            sqlite_where=text("transaction_id IS NOT NULL"),
        ),
        Index(
            "ux_anchor_events_global_prev",
            "prev_event_hash",
            unique=True,
            postgresql_where=text("transaction_id IS NULL"),
            sqlite_where=text("transaction_id IS NULL"),
        ),
        Index("ix_anchor_events_unbatched", "id", postgresql_where=text("seq IS NULL")),
    )

    id: Mapped[int] = mapped_column(_BIGINT_ID, primary_key=True, autoincrement=True)
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    source_table: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    salt: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    prev_event_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    leaf_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    batch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    leaf_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


install_guards(AnchorEvent.__table__)
