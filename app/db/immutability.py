"""Append-only guards for the money history (M21 A3, decision D7).

`ledger_entries` and `money_audit_events` reject every UPDATE, DELETE and TRUNCATE.
`anchor_events` rejects them too, except the batcher's one-time assignment of `seq`, `batch_id`
and `leaf_index` from NULL with every other column unchanged.

The triggers are attached to the tables' `after_create` events, so `Base.metadata.create_all`
installs them in SQLite (the test suite) and PostgreSQL alike. The Alembic migration
(`f4c2a9e1b7d3`) carries its own frozen copy of the PostgreSQL statements.

These triggers stop bugs and casual edits. They do not stop the table owner (`ALTER TABLE ...
DISABLE TRIGGER`) or a superuser; the anchored Merkle roots are what catch those.
"""

from __future__ import annotations

from sqlalchemy import DDL, Table, event

APPEND_ONLY_TABLES = ("ledger_entries", "money_audit_events")
ANCHOR_EVENTS_TABLE = "anchor_events"

# Columns that never change once an anchor event is written.
ANCHOR_EVENT_FIXED_COLUMNS = (
    "id",
    "transaction_id",
    "event_type",
    "source_table",
    "source_id",
    "schema_version",
    "payload",
    "salt",
    "prev_event_hash",
    "leaf_hash",
    "created_at",
)
ANCHOR_EVENT_BATCH_COLUMNS = ("seq", "batch_id", "leaf_index")


def _postgres_append_only(table: str) -> list[str]:
    return [
        f"""
        CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '{table} is append-only (% refused)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """,
        f"""
        CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION {table}_append_only()
        """,
        f"""
        CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
        FOR EACH STATEMENT EXECUTE FUNCTION {table}_append_only()
        """,
    ]


def _postgres_anchor_events() -> list[str]:
    fixed_new = ", ".join(f"NEW.{column}" for column in ANCHOR_EVENT_FIXED_COLUMNS)
    fixed_old = ", ".join(f"OLD.{column}" for column in ANCHOR_EVENT_FIXED_COLUMNS)
    unset = " AND ".join(f"OLD.{column} IS NULL" for column in ANCHOR_EVENT_BATCH_COLUMNS)
    now_set = " AND ".join(f"NEW.{column} IS NOT NULL" for column in ANCHOR_EVENT_BATCH_COLUMNS)
    # NEW is not set for DELETE, so the UPDATE check is nested rather than AND-ed.
    return [
        f"""
        CREATE OR REPLACE FUNCTION anchor_events_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF {unset} AND {now_set}
                   AND ROW({fixed_new}) IS NOT DISTINCT FROM ROW({fixed_old}) THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'anchor_events is append-only (% refused)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """,
        """
        CREATE TRIGGER anchor_events_guard BEFORE UPDATE OR DELETE ON anchor_events
        FOR EACH ROW EXECUTE FUNCTION anchor_events_guard()
        """,
        *_postgres_append_only(ANCHOR_EVENTS_TABLE)[0:1],
        f"""
        CREATE TRIGGER {ANCHOR_EVENTS_TABLE}_no_truncate BEFORE TRUNCATE ON {ANCHOR_EVENTS_TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {ANCHOR_EVENTS_TABLE}_append_only()
        """,
    ]


def _sqlite_append_only(table: str) -> list[str]:
    return [
        f"""
        CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, '{table} is append-only (UPDATE refused)'); END
        """,
        f"""
        CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{table} is append-only (DELETE refused)'); END
        """,
    ]


def _sqlite_anchor_events() -> list[str]:
    unchanged = " AND ".join(
        f"NEW.{column} IS OLD.{column}" for column in ANCHOR_EVENT_FIXED_COLUMNS
    )
    unset = " AND ".join(f"OLD.{column} IS NULL" for column in ANCHOR_EVENT_BATCH_COLUMNS)
    now_set = " AND ".join(f"NEW.{column} IS NOT NULL" for column in ANCHOR_EVENT_BATCH_COLUMNS)
    return [
        f"""
        CREATE TRIGGER anchor_events_guard_update BEFORE UPDATE ON anchor_events
        WHEN NOT ({unset} AND {now_set} AND {unchanged})
        BEGIN SELECT RAISE(ABORT, 'anchor_events is append-only (UPDATE refused)'); END
        """,
        """
        CREATE TRIGGER anchor_events_guard_delete BEFORE DELETE ON anchor_events
        BEGIN SELECT RAISE(ABORT, 'anchor_events is append-only (DELETE refused)'); END
        """,
    ]


def postgres_statements(table: str) -> list[str]:
    if table == ANCHOR_EVENTS_TABLE:
        return _postgres_anchor_events()
    return _postgres_append_only(table)


def sqlite_statements(table: str) -> list[str]:
    if table == ANCHOR_EVENTS_TABLE:
        return _sqlite_anchor_events()
    return _sqlite_append_only(table)


def install_guards(table: Table) -> None:
    """Attach the guard DDL to ``table`` so every create_all installs it."""
    for statement in postgres_statements(table.name):
        event.listen(table, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for statement in sqlite_statements(table.name):
        event.listen(table, "after_create", DDL(statement).execute_if(dialect="sqlite"))
