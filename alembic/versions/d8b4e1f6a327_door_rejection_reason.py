"""door rejection reason and note on transactions

Revision ID: d8b4e1f6a327
Revises: c3a7d9e4f215
Create Date: 2026-10-20 00:00:02.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8b4e1f6a327"
down_revision: Union[str, Sequence[str], None] = "c3a7d9e4f215"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("door_rejection_reason", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "transactions", sa.Column("door_rejection_note", sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("transactions", "door_rejection_note")
    op.drop_column("transactions", "door_rejection_reason")
