"""m4 add delivery otp tracking columns

Revision ID: ebc2f6a1d4a3
Revises: 8e02d307f2f1
Create Date: 2026-09-21 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "ebc2f6a1d4a3"
down_revision: Union[str, Sequence[str], None] = "8e02d307f2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions", sa.Column("delivery_otp_hash", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "transactions",
        sa.Column("otp_failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "ck_transactions_otp_failed_attempts_non_negative",
        "transactions",
        "otp_failed_attempts >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_transactions_otp_failed_attempts_non_negative",
        "transactions",
        type_="check",
    )
    op.drop_column("transactions", "otp_failed_attempts")
    op.drop_column("transactions", "delivery_otp_hash")
