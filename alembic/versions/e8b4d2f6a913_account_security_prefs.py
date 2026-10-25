"""session devices and notification preferences

Revision ID: e8b4d2f6a913
Revises: d5a2c7e9b318
Create Date: 2026-10-26 00:00:02.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8b4d2f6a913"
down_revision: Union[str, Sequence[str], None] = "d5a2c7e9b318"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_sessions", sa.Column("user_agent", sa.String(length=255), nullable=True))
    op.add_column(
        "user_sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("notification_prefs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "notification_prefs")
    op.drop_column("user_sessions", "last_seen_at")
    op.drop_column("user_sessions", "user_agent")
