"""m3 set transaction initial state to awaiting payment

Revision ID: 3d0f8f4dcf21
Revises: b6a85a0894b4
Create Date: 2026-09-20 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3d0f8f4dcf21"
down_revision: Union[str, Sequence[str], None] = "b6a85a0894b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'awaiting_payment'")
    op.execute(
        "ALTER TABLE transactions ALTER COLUMN status SET DEFAULT 'awaiting_payment'::transaction_status"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE transactions ALTER COLUMN status SET DEFAULT 'initiated'::transaction_status"
    )
