"""m24 liabilities snapshots

Tables for proof of liabilities (#261): ``liabilities_snapshots`` (the sum tree's root, total held
in cents and leaf count, linked to the anchored global event) and ``liabilities_leaves`` (one salted
leaf per open escrow, so the order's buyer can be served their proof).

Revision ID: e5a9c3d1f7b2
Revises: d7e2b5c9a1f3
Create Date: 2026-11-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a9c3d1f7b2"
down_revision: Union[str, Sequence[str], None] = "d7e2b5c9a1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "liabilities_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("root_hash", sa.LargeBinary(length=32), nullable=True),
        sa.Column("total_cents", sa.Numeric(20, 0), nullable=False),
        sa.Column("leaf_count", sa.Integer(), nullable=False),
        # No foreign key: anchor_events is append-only and must stay free of inbound references
        # (its triggers, not a constraint, are what refuse TRUNCATE).
        sa.Column("anchor_event_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("total_cents >= 0", name="ck_liabilities_snapshots_total_non_negative"),
    )
    op.create_table(
        "liabilities_leaves",
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("liabilities_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("leaf_index", sa.Integer(), primary_key=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column("salt", sa.LargeBinary(length=32), nullable=False),
        sa.Column("order_commit", sa.LargeBinary(length=32), nullable=False),
        sa.Column("amount_cents", sa.Numeric(20, 0), nullable=False),
        sa.CheckConstraint("amount_cents >= 0", name="ck_liabilities_leaves_amount_non_negative"),
    )
    op.create_index(
        "ix_liabilities_leaves_transaction_id", "liabilities_leaves", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_liabilities_leaves_transaction_id", table_name="liabilities_leaves")
    op.drop_table("liabilities_leaves")
    op.drop_table("liabilities_snapshots")
