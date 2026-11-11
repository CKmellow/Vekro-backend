"""m28 store events

``store_events``: a store's open/close history (append-only), each row anchored as
``store_opened`` or ``store_closed`` in the global chain. Stores from before M28 get their
``opened`` row from the ``backfill-store-events`` CLI, never from this migration.

Revision ID: d3f5b7c9e1a2
Revises: c2e4a6b8d0f1
Create Date: 2026-11-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3f5b7c9e1a2"
down_revision: Union[str, Sequence[str], None] = "c2e4a6b8d0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "store_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reopened", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("migrated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('opened', 'closed')", name="ck_store_events_kind"),
    )
    op.create_index("ix_store_events_user_id", "store_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_store_events_user_id", table_name="store_events")
    op.drop_table("store_events")
