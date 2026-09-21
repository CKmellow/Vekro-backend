"""m4 add out_for_delivery transaction status

Revision ID: 8e02d307f2f1
Revises: 3d0f8f4dcf21
Create Date: 2026-09-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e02d307f2f1"
down_revision: Union[str, Sequence[str], None] = "3d0f8f4dcf21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE transaction_status ADD VALUE IF NOT EXISTS 'out_for_delivery'")


def downgrade() -> None:
    # PostgreSQL enums cannot safely drop values in place.
    pass
