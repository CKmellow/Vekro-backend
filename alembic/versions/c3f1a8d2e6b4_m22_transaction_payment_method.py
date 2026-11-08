"""m22 transaction payment method

Additive column on ``transactions`` (#251): how the buyer chose to pay, ``mpesa`` (STK prompt) or
``card`` (Pesapal hosted checkout). It decides how a refund can be paid back: card money cannot be
refunded over M-Pesa. Existing rows default to 'mpesa'.

Revision ID: c3f1a8d2e6b4
Revises: a5d3c8e2f914
Create Date: 2026-11-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f1a8d2e6b4"
down_revision: Union[str, Sequence[str], None] = "a5d3c8e2f914"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "payment_method",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'mpesa'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("transactions", "payment_method")
