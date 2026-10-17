"""m20 econfirm escrow reference and reconciliation flag

Additive, nullable/default-backed columns on ``escrows`` for E5 orchestration:
  * econfirm_transaction_id       - eConfirm's own txn id (provider reference)
  * econfirm_needs_reconciliation - set when a create returned UNKNOWN (A3)

No existing data is modified; existing rows default to needs_reconciliation=false.

Revision ID: d1a73be5c820
Revises: c9f2a04d6b71
Create Date: 2026-10-17 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1a73be5c820"
down_revision: Union[str, Sequence[str], None] = "c9f2a04d6b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "escrows",
        sa.Column("econfirm_transaction_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "escrows",
        sa.Column(
            "econfirm_needs_reconciliation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("escrows", "econfirm_needs_reconciliation")
    op.drop_column("escrows", "econfirm_transaction_id")
