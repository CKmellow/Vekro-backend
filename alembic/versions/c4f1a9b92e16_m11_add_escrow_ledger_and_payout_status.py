"""m11 add escrow ledger and payout status

Revision ID: c4f1a9b92e16
Revises: ebc2f6a1d4a3
Create Date: 2026-09-29 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4f1a9b92e16"
down_revision: Union[str, Sequence[str], None] = "ebc2f6a1d4a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


transaction_payout_status_enum = sa.Enum(
    "not_required",
    "pending",
    "succeeded",
    "failed_definite",
    "unknown",
    name="transaction_payout_status",
)

ledger_entry_side_enum = sa.Enum(
    "debit",
    "credit",
    name="ledger_entry_side",
)


def upgrade() -> None:
    transaction_payout_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "transactions",
        sa.Column(
            "payout_status",
            transaction_payout_status_enum,
            nullable=False,
            server_default=sa.text("'not_required'"),
        ),
    )
    op.execute("UPDATE transactions SET payout_status = 'not_required' WHERE payout_status IS NULL")
    op.create_index(op.f("ix_transactions_payout_status"), "transactions", ["payout_status"])

    op.create_table(
        "escrows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column("buyer_id", sa.UUID(), nullable=False),
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("amount_total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "funded_amount",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "released_amount",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "refunded_amount",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=sa.text("'KES'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_total > 0", name="ck_escrows_amount_total_positive"),
        sa.CheckConstraint("funded_amount >= 0", name="ck_escrows_funded_amount_non_negative"),
        sa.CheckConstraint("released_amount >= 0", name="ck_escrows_released_amount_non_negative"),
        sa.CheckConstraint("refunded_amount >= 0", name="ck_escrows_refunded_amount_non_negative"),
        sa.CheckConstraint("funded_amount <= amount_total", name="ck_escrows_funded_le_total"),
        sa.CheckConstraint(
            "released_amount + refunded_amount <= funded_amount",
            name="ck_escrows_outflows_le_funded",
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["buyer_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["seller_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_escrows_transaction_id"), "escrows", ["transaction_id"], unique=True)
    op.create_index(op.f("ix_escrows_buyer_id"), "escrows", ["buyer_id"], unique=False)
    op.create_index(op.f("ix_escrows_seller_id"), "escrows", ["seller_id"], unique=False)

    ledger_entry_side_enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("escrow_id", sa.UUID(), nullable=False),
        sa.Column("entry_group_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("account_code", sa.String(length=64), nullable=False),
        sa.Column("side", ledger_entry_side_enum, nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=sa.text("'KES'")),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        sa.ForeignKeyConstraint(["escrow_id"], ["escrows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ledger_entries_escrow_id"), "ledger_entries", ["escrow_id"])
    op.create_index(op.f("ix_ledger_entries_entry_group_id"), "ledger_entries", ["entry_group_id"])
    op.create_index(
        op.f("ix_ledger_entries_idempotency_key"), "ledger_entries", ["idempotency_key"]
    )
    op.create_index(op.f("ix_ledger_entries_side"), "ledger_entries", ["side"])


def downgrade() -> None:
    op.drop_index(op.f("ix_ledger_entries_side"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_idempotency_key"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_entry_group_id"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_escrow_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
    ledger_entry_side_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_index(op.f("ix_escrows_seller_id"), table_name="escrows")
    op.drop_index(op.f("ix_escrows_buyer_id"), table_name="escrows")
    op.drop_index(op.f("ix_escrows_transaction_id"), table_name="escrows")
    op.drop_table("escrows")

    op.drop_index(op.f("ix_transactions_payout_status"), table_name="transactions")
    op.drop_column("transactions", "payout_status")
    transaction_payout_status_enum.drop(op.get_bind(), checkfirst=True)
