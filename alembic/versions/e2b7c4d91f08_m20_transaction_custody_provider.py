"""m20 transaction custody provider

Additive column on ``transactions`` for E7: which custody provider holds the
transaction's money, exposed on the API so the UI never presents a simulated
escrow as real money (Hard Rule 2). Existing rows default to 'simulated'.

Revision ID: e2b7c4d91f08
Revises: b5e8f1c2a904
Create Date: 2026-10-20 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c4d91f08"
down_revision: Union[str, Sequence[str], None] = "b5e8f1c2a904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "custody_provider",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'simulated'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("transactions", "custody_provider")
