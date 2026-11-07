"""One Merkle batch of anchor events and its journey onto the AnchorRegistry (M21 A5).

`id` is the on-chain batch id (1, 2, ...). The batch's identity (`root`, `prev_root`, `first_seq`,
`last_seq`, `leaf_count`) is fixed when it is built and never changes: a retry, a fee bump or a
reorg resubmits exactly the same values. The rest is the submitter's working state.

Statuses: BUILT (ready to submit), SUBMITTED (a signed transaction is stored and broadcast),
CONFIRMED (on-chain root matches, with enough confirmations), FAILED_RETRYABLE (the last attempt
failed in a way a retry can fix), HALTED (the chain disagrees with the database; a person must
look, and nothing more is anchored until then).
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Enum, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnchorBatchStatus(StrEnum):
    BUILT = "built"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED_RETRYABLE = "failed_retryable"
    HALTED = "halted"


IN_FLIGHT_STATUSES = (
    AnchorBatchStatus.BUILT,
    AnchorBatchStatus.SUBMITTED,
    AnchorBatchStatus.FAILED_RETRYABLE,
)


class AnchorBatch(Base):
    __tablename__ = "anchor_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    root: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    prev_root: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    first_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    leaf_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AnchorBatchStatus] = mapped_column(
        Enum(
            AnchorBatchStatus,
            name="anchor_batch_status",
            values_callable=lambda members: [m.value for m in members],
        ),
        nullable=False,
        default=AnchorBatchStatus.BUILT,
        index=True,
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registry_address: Mapped[str] = mapped_column(String(42), nullable=False)

    # The latest signed transaction, stored before it is broadcast so a crash can rebroadcast it.
    tx_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    raw_tx: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    nonce: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_fee_per_gas: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_priority_fee_per_gas: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Every hash broadcast for this batch (fee bumps replace the transaction at the same nonce).
    tx_hashes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    halt_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    block_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
