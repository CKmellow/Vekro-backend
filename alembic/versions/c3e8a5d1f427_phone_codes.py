"""phone sign-in codes

Revision ID: c3e8a5d1f427
Revises: b7d3f0a1c822
Create Date: 2026-10-25 00:00:02.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e8a5d1f427"
down_revision: Union[str, Sequence[str], None] = "b7d3f0a1c822"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phone_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_phone_codes_phone", "phone_codes", ["phone"])
    op.create_index("ix_phone_codes_created_at", "phone_codes", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_phone_codes_created_at", table_name="phone_codes")
    op.drop_index("ix_phone_codes_phone", table_name="phone_codes")
    op.drop_table("phone_codes")
