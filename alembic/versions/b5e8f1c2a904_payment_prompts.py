"""payment prompts: instruction kind, failure reason and resend tracking

Revision ID: b5e8f1c2a904
Revises: d1a73be5c820
Create Date: 2026-10-20 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5e8f1c2a904"
down_revision: Union[str, Sequence[str], None] = "d1a73be5c820"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_prompts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=32), nullable=True),
        sa.Column("redirect_url", sa.String(length=1024), nullable=True),
        sa.Column("checkout_request_id", sa.String(length=120), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_payment_prompts_transaction_id"), "payment_prompts", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_prompts_transaction_id"), table_name="payment_prompts")
    op.drop_table("payment_prompts")
