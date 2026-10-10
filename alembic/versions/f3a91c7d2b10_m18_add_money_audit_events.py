"""m18 add money audit events

Revision ID: f3a91c7d2b10
Revises: e7f99b6cb3ea
Create Date: 2026-10-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3a91c7d2b10"
down_revision: Union[str, Sequence[str], None] = "e7f99b6cb3ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "money_audit_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("transaction_id", sa.UUID(), nullable=True),
        sa.Column("escrow_id", sa.UUID(), nullable=True),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("provider_reference", sa.String(length=120), nullable=True),
        sa.Column("rail_name", sa.String(length=40), nullable=True),
        sa.Column("correlation_id", sa.String(length=320), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_money_audit_events_occurred_at", "money_audit_events", ["occurred_at"])
    op.create_index(
        "ix_money_audit_events_transaction_id", "money_audit_events", ["transaction_id"]
    )
    op.create_index(
        "ix_money_audit_events_provider_reference",
        "money_audit_events",
        ["provider_reference"],
    )
    op.create_index("ix_money_audit_events_action", "money_audit_events", ["action"])


def downgrade() -> None:
    op.drop_index("ix_money_audit_events_action", table_name="money_audit_events")
    op.drop_index("ix_money_audit_events_provider_reference", table_name="money_audit_events")
    op.drop_index("ix_money_audit_events_transaction_id", table_name="money_audit_events")
    op.drop_index("ix_money_audit_events_occurred_at", table_name="money_audit_events")
    op.drop_table("money_audit_events")
