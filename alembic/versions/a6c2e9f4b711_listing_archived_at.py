"""listing archived_at

Revision ID: a6c2e9f4b711
Revises: d8b4e1f6a327
Create Date: 2026-10-24 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6c2e9f4b711"
down_revision: Union[str, Sequence[str], None] = "d8b4e1f6a327"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set while the seller has archived the listing; its link takes no payments.",
        ),
    )


def downgrade() -> None:
    op.drop_column("listings", "archived_at")
