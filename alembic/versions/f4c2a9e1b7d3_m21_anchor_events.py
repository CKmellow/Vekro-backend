"""anchor_events log and append-only guards on the money history (M21 A3)

Revision ID: f4c2a9e1b7d3
Revises: e8b4d2f6a913
Create Date: 2026-10-31 00:00:00.000000

Creates anchor_events and installs triggers that refuse UPDATE, DELETE and TRUNCATE on
ledger_entries, money_audit_events and anchor_events. The one exception is the batcher's one-time
assignment of anchor_events.seq/batch_id/leaf_index (docs/anchoring-design.md 5.4, decision D7).
The statements are a frozen copy of app/db/immutability.py at this revision.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4c2a9e1b7d3"
down_revision: Union[str, Sequence[str], None] = "e8b4d2f6a913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GUARD_STATEMENTS = [
    "CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger_entries is append-only (% refused)', TG_OP USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only()",
    "CREATE TRIGGER ledger_entries_no_truncate BEFORE TRUNCATE ON ledger_entries FOR EACH STATEMENT EXECUTE FUNCTION ledger_entries_append_only()",
    "CREATE OR REPLACE FUNCTION money_audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'money_audit_events is append-only (% refused)', TG_OP USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER money_audit_events_append_only BEFORE UPDATE OR DELETE ON money_audit_events FOR EACH ROW EXECUTE FUNCTION money_audit_events_append_only()",
    "CREATE TRIGGER money_audit_events_no_truncate BEFORE TRUNCATE ON money_audit_events FOR EACH STATEMENT EXECUTE FUNCTION money_audit_events_append_only()",
    "CREATE OR REPLACE FUNCTION anchor_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF TG_OP = 'UPDATE' THEN IF OLD.seq IS NULL AND OLD.batch_id IS NULL AND OLD.leaf_index IS NULL AND NEW.seq IS NOT NULL AND NEW.batch_id IS NOT NULL AND NEW.leaf_index IS NOT NULL AND ROW(NEW.id, NEW.transaction_id, NEW.event_type, NEW.source_table, NEW.source_id, NEW.schema_version, NEW.payload, NEW.salt, NEW.prev_event_hash, NEW.leaf_hash, NEW.created_at) IS NOT DISTINCT FROM ROW(OLD.id, OLD.transaction_id, OLD.event_type, OLD.source_table, OLD.source_id, OLD.schema_version, OLD.payload, OLD.salt, OLD.prev_event_hash, OLD.leaf_hash, OLD.created_at) THEN RETURN NEW; END IF; END IF; RAISE EXCEPTION 'anchor_events is append-only (% refused)', TG_OP USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER anchor_events_guard BEFORE UPDATE OR DELETE ON anchor_events FOR EACH ROW EXECUTE FUNCTION anchor_events_guard()",
    "CREATE OR REPLACE FUNCTION anchor_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'anchor_events is append-only (% refused)', TG_OP USING ERRCODE = 'restrict_violation'; END $$",
    "CREATE TRIGGER anchor_events_no_truncate BEFORE TRUNCATE ON anchor_events FOR EACH STATEMENT EXECUTE FUNCTION anchor_events_append_only()",
]

GUARDED_TABLES = ("ledger_entries", "money_audit_events", "anchor_events")
GUARD_FUNCTIONS = (
    "ledger_entries_append_only",
    "money_audit_events_append_only",
    "anchor_events_guard",
    "anchor_events_append_only",
)
GUARD_TRIGGERS = (
    ("ledger_entries", "ledger_entries_append_only"),
    ("ledger_entries", "ledger_entries_no_truncate"),
    ("money_audit_events", "money_audit_events_append_only"),
    ("money_audit_events", "money_audit_events_no_truncate"),
    ("anchor_events", "anchor_events_guard"),
    ("anchor_events", "anchor_events_no_truncate"),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "anchor_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("source_table", sa.String(length=48), nullable=True),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("salt", sa.LargeBinary(length=32), nullable=False),
        sa.Column("prev_event_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("leaf_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=True),
        sa.Column("leaf_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_table", "source_id", name="uq_anchor_events_source"),
        sa.UniqueConstraint("seq", name="uq_anchor_events_seq"),
    )
    op.create_index("ix_anchor_events_transaction_id", "anchor_events", ["transaction_id"])
    op.create_index(
        "ux_anchor_events_txn_prev",
        "anchor_events",
        ["transaction_id", "prev_event_hash"],
        unique=True,
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )
    op.create_index(
        "ux_anchor_events_global_prev",
        "anchor_events",
        ["prev_event_hash"],
        unique=True,
        postgresql_where=sa.text("transaction_id IS NULL"),
    )
    op.create_index(
        "ix_anchor_events_unbatched",
        "anchor_events",
        ["id"],
        postgresql_where=sa.text("seq IS NULL"),
    )
    if _is_postgres():
        for statement in GUARD_STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    if _is_postgres():
        for table, trigger in GUARD_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in GUARD_FUNCTIONS:
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index("ix_anchor_events_unbatched", table_name="anchor_events")
    op.drop_index("ux_anchor_events_global_prev", table_name="anchor_events")
    op.drop_index("ux_anchor_events_txn_prev", table_name="anchor_events")
    op.drop_index("ix_anchor_events_transaction_id", table_name="anchor_events")
    op.drop_table("anchor_events")
