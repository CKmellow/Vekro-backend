"""m23 ussd

USSD (#263): a hashed USSD PIN on users with its own lockout, and ``ussd_sessions`` keyed by
Africa's Talking's sessionId (the order ids behind each numbered menu line, and an expiry).

Revision ID: f1b8d4e6a2c9
Revises: e5a9c3d1f7b2
Create Date: 2026-11-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1b8d4e6a2c9"
down_revision: Union[str, Sequence[str], None] = "e5a9c3d1f7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("ussd_pin_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column("ussd_pin_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users", sa.Column("ussd_pin_locked_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "ussd_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ussd_sessions_expires_at", "ussd_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_ussd_sessions_expires_at", table_name="ussd_sessions")
    op.drop_table("ussd_sessions")
    op.drop_column("users", "ussd_pin_locked_until")
    op.drop_column("users", "ussd_pin_failures")
    op.drop_column("users", "ussd_pin_hash")
