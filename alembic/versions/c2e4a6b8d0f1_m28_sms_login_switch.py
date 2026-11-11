"""m28 sms login switch

``users.sms_login_enabled``: a member's own switch for signing in with an SMS code (default on).
Admins never sign in by SMS code whatever it says (docs/accounts-and-modes.md, section 3).

Revision ID: c2e4a6b8d0f1
Revises: b8d1f3a5c7e2
Create Date: 2026-11-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: Union[str, Sequence[str], None] = "b8d1f3a5c7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("sms_login_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("users", "sms_login_enabled")
