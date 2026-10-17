"""E4: the M20 escrow-custody migration is additive and preserves existing rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "c9f2a04d6b71_m20_add_escrow_custody_columns.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m20_escrow_custody", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chains_from_previous_head():
    module = _load_migration()
    assert module.revision == "c9f2a04d6b71"
    assert module.down_revision == "b8d15e3f7a42"


def test_existing_simulated_escrow_survives_upgrade():
    module = _load_migration()
    engine = create_engine("sqlite://")

    with engine.begin() as conn:
        # Legacy-shaped escrows table with one pre-existing row.
        conn.execute(text("CREATE TABLE escrows (id TEXT PRIMARY KEY, amount_total NUMERIC)"))
        conn.execute(text("INSERT INTO escrows (id, amount_total) VALUES ('legacy-1', 2000)"))

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        row = conn.execute(
            text(
                "SELECT amount_total, custody_provider, econfirm_confirmation_code, "
                "econfirm_payer_phone FROM escrows WHERE id = 'legacy-1'"
            )
        ).fetchone()

    # Row untouched except the new columns, which default to simulated/NULL.
    assert row[0] == 2000
    assert row[1] == "simulated"
    assert row[2] is None
    assert row[3] is None


def _load_by_path(name: str):
    path = MIGRATION.parent / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e5_reference_migration_adds_columns_and_preserves_rows():
    first = _load_migration()  # c9f2... (E4)
    second = _load_by_path("d1a73be5c820_m20_econfirm_escrow_reference.py")  # E5
    assert second.down_revision == first.revision

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE escrows (id TEXT PRIMARY KEY, amount_total NUMERIC)"))
        conn.execute(text("INSERT INTO escrows (id, amount_total) VALUES ('legacy-9', 300)"))

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            first.upgrade()
            second.upgrade()
        conn.commit()
        row = conn.execute(
            text(
                "SELECT amount_total, custody_provider, econfirm_transaction_id, "
                "econfirm_needs_reconciliation FROM escrows WHERE id = 'legacy-9'"
            )
        ).fetchone()

    assert row[0] == 300
    assert row[1] == "simulated"
    assert row[2] is None
    assert row[3] in (0, False)  # defaulted, not flagged


def test_downgrade_removes_the_columns():
    module = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE escrows (id TEXT PRIMARY KEY, amount_total NUMERIC)"))

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.downgrade()
        conn.commit()
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(escrows)")).fetchall()}

    assert "custody_provider" not in cols
    assert "econfirm_confirmation_code" not in cols
