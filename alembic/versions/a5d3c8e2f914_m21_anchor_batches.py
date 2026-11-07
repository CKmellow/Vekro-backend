"""anchor_batches: Merkle batches and their submission to the AnchorRegistry (M21 A5)

Revision ID: a5d3c8e2f914
Revises: f4c2a9e1b7d3
Create Date: 2026-11-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a5d3c8e2f914"
down_revision: Union[str, Sequence[str], None] = "f4c2a9e1b7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False: the type is created once, explicitly and idempotently, in upgrade().
anchor_batch_status_enum = postgresql.ENUM(
    "built",
    "submitted",
    "confirmed",
    "failed_retryable",
    "halted",
    name="anchor_batch_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        anchor_batch_status_enum.create(bind, checkfirst=True)
        status_type: sa.types.TypeEngine = anchor_batch_status_enum
    else:
        status_type = sa.Enum(
            "built",
            "submitted",
            "confirmed",
            "failed_retryable",
            "halted",
            name="anchor_batch_status",
        )

    op.create_table(
        "anchor_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("root", sa.LargeBinary(length=32), nullable=False),
        sa.Column("prev_root", sa.LargeBinary(length=32), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("leaf_count", sa.Integer(), nullable=False),
        sa.Column("status", status_type, nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("registry_address", sa.String(length=42), nullable=False),
        sa.Column("tx_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("raw_tx", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.BigInteger(), nullable=True),
        sa.Column("max_fee_per_gas", sa.BigInteger(), nullable=True),
        sa.Column("max_priority_fee_per_gas", sa.BigInteger(), nullable=True),
        sa.Column("tx_hashes", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("halt_reason", sa.String(length=500), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("block_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root"),
    )
    op.create_index("ix_anchor_batches_status", "anchor_batches", ["status"])


def downgrade() -> None:
    op.drop_index("ix_anchor_batches_status", table_name="anchor_batches")
    op.drop_table("anchor_batches")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        anchor_batch_status_enum.drop(bind, checkfirst=True)
