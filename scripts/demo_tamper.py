#!/usr/bin/env python3
"""Tamper demo for the anchoring defence (M21 A6). CORRUPTS DATA ON PURPOSE.

    ANCHOR_ENABLED=false ANCHOR_GATE_PAYOUTS=false ANCHOR_REGISTRY_ADDRESS= \\
        .venv/bin/python scripts/demo_tamper.py <scenario> --reference VK-XXXXXX \\
        --from-db vekro_base_sepolia --i-understand-this-corrupts-data

It never edits the database it is pointed at. It copies --from-db to <name>_tamper_copy
(CREATE DATABASE ... TEMPLATE, PostgreSQL only), prints the copy's name, and edits only the copy.
--from-db <name>_tamper_copy keeps editing an existing copy; --fresh-copy replaces it.

Scenarios, each what an insider with database access might try:

- edit-ledger:         raise the amount on the order's first ledger row
- delete-middle-event: delete an event from the middle of the order's chain
- delete-last-event:   delete the order's latest event (only the full audit can tell)
- change-status:       set the order's status without recording a transition

The append-only triggers are switched off around the edit, as a table owner could do
(PostgreSQL: ALTER TABLE ... DISABLE TRIGGER USER; SQLite: drop and recreate). Afterwards run
scripts/verify_transaction.py and scripts/audit_anchoring.py to watch them catch it.

Refuses (#279): with ENVIRONMENT=production; without the confirmation flag; when
ANCHOR_REGISTRY_ADDRESS is a public-chain registry listed in contracts/deployments/*.json (or that
list cannot be read); and for any database whose name does not end in _tamper_copy. Afterwards
check the copy against the public roots with the normal settings:
scripts/verify_transaction.py --database <copy> and scripts/audit_anchoring.py --database <copy>.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

SCENARIOS = ("edit-ledger", "delete-middle-event", "delete-last-event", "change-status")
CONFIRM_FLAG = "--i-understand-this-corrupts-data"
COPY_SUFFIX = "_tamper_copy"
DEPLOYMENTS = Path(__file__).resolve().parents[1] / "contracts" / "deployments"
LOCAL_CHAIN_IDS = {31337}
# How to run the demo with anchoring off for this process only (the copy is checked afterwards).
LOCAL_ONLY = "ANCHOR_ENABLED=false ANCHOR_GATE_PAYOUTS=false ANCHOR_REGISTRY_ADDRESS="
_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@contextmanager
def triggers_off(db: Session, table: str) -> Iterator[None]:
    """What a table owner can do; the anchored roots are what catch it."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
        try:
            yield
        finally:
            db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
        return
    saved = list(
        db.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :t"),
            {"t": table},
        )
    )
    for name, _sql in saved:
        db.execute(text(f'DROP TRIGGER "{name}"'))
    try:
        yield
    finally:
        for _name, sql in saved:
            db.execute(text(sql))


def tamper(db: Session, scenario: str, reference: str) -> str:
    from app.models.anchor_event import AnchorEvent
    from app.models.escrow import Escrow
    from app.models.ledger_entry import LedgerEntry
    from app.models.transaction import Transaction, TransactionStatus

    transaction = db.execute(
        select(Transaction).where(Transaction.reference == reference)
    ).scalar_one_or_none()
    if transaction is None:
        raise LookupError(f"No order {reference}.")

    if scenario == "edit-ledger":
        entry = db.execute(
            select(LedgerEntry)
            .join(Escrow, Escrow.id == LedgerEntry.escrow_id)
            .where(Escrow.transaction_id == transaction.id)
            .order_by(LedgerEntry.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if entry is None:
            raise LookupError("The order has no ledger rows yet.")
        old_amount = entry.amount
        new_amount = old_amount * 10 + Decimal("1.00")
        with triggers_off(db, "ledger_entries"):
            db.execute(
                update(LedgerEntry).where(LedgerEntry.id == entry.id).values(amount=new_amount)
            )
        detail = f"ledger row {entry.id}: {old_amount} -> {new_amount}"
    elif scenario in ("delete-middle-event", "delete-last-event"):
        events = list(
            db.execute(
                select(AnchorEvent.id)
                .where(AnchorEvent.transaction_id == transaction.id)
                .order_by(AnchorEvent.id)
            ).scalars()
        )
        if len(events) < 3:
            raise LookupError("The order needs at least three events for this demo.")
        victim = events[len(events) // 2] if scenario == "delete-middle-event" else events[-1]
        with triggers_off(db, "anchor_events"):
            db.execute(delete(AnchorEvent).where(AnchorEvent.id == victim))
        detail = f"anchor event {victim} deleted"
    elif scenario == "change-status":
        old_status = transaction.status
        target = (
            TransactionStatus.RELEASED
            if transaction.status != TransactionStatus.RELEASED
            else TransactionStatus.REFUNDED_BUYER
        )
        # A raw UPDATE: the ORM flush guard would otherwise record a transition.
        db.execute(
            update(Transaction).where(Transaction.id == transaction.id).values(status=target)
        )
        detail = f"status {old_status.value} -> {target.value} with no transition"
    else:  # pragma: no cover - argparse limits the choices
        raise ValueError(scenario)
    db.commit()
    return detail


def public_registries(directory: Path = DEPLOYMENTS) -> dict[str, str]:
    """Registry address (lower case) -> deployment file, for every non-local chain."""
    registries: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("chainId", 0)) not in LOCAL_CHAIN_IDS and data.get("registry"):
            registries[str(data["registry"]).lower()] = path.name
    return registries


def copy_name_for(from_db: str) -> str:
    return from_db if from_db.endswith(COPY_SUFFIX) else f"{from_db}{COPY_SUFFIX}"


def refusal(
    *,
    environment: str,
    registry_address: str,
    target: str,
    dialect: str,
    deployments: Path = DEPLOYMENTS,
) -> str | None:
    """Why this demo must not run, or None. Checked before any database is created or touched."""
    if environment.strip().lower() == "production":
        return "this demo never runs in production."
    try:
        public = public_registries(deployments)
    except (OSError, ValueError) as exc:
        return f"cannot read the public registry list in {deployments} ({type(exc).__name__})."
    if not public:
        return f"no public registry list found in {deployments}; refusing rather than guessing."
    deployment = public.get(registry_address.strip().lower())
    if deployment is not None:
        return (
            f"ANCHOR_REGISTRY_ADDRESS is the public registry in contracts/deployments/{deployment}."
            f" Run this script with {LOCAL_ONLY}; verify_transaction.py and audit_anchoring.py "
            "read the public roots afterwards."
        )
    if dialect != "postgresql":
        return "this demo needs PostgreSQL (it copies the database first)."
    if not _DB_NAME.match(target) or not target.endswith(COPY_SUFFIX):
        return f"{target!r} is not a tamper copy (the name must end in {COPY_SUFFIX})."
    return None


def _copy_database(source: Any, from_db: str, copy: str, *, fresh: bool) -> str | None:
    """Create ``copy`` from ``from_db`` on the same server. Returns an error, or None."""
    from sqlalchemy import create_engine

    admin = create_engine(source.with_name("postgres").url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            names = set(conn.execute(text("SELECT datname FROM pg_database")).scalars())
            if from_db not in names:
                return f"database {from_db} does not exist."
            if copy in names:
                if not fresh:
                    return (
                        f"{copy} already exists. Pass --fresh-copy to replace it, or "
                        f"--from-db {copy} to keep editing it."
                    )
                print(f"Dropping the old copy {copy}.", file=sys.stderr)
                conn.execute(text(f'DROP DATABASE "{copy}"'))
            print(f"Copying database {from_db} to {copy}.", file=sys.stderr)
            try:
                conn.execute(text(f'CREATE DATABASE "{copy}" TEMPLATE "{from_db}"'))
            except Exception as exc:  # most often: someone is connected to the source
                return (
                    f"could not copy {from_db} ({type(exc).__name__}). PostgreSQL copies only a "
                    "database nobody is connected to: stop the API, worker and scheduler first."
                )
    finally:
        admin.dispose()
    return None


def _session_for(target: Any) -> Session:
    from app.db.session import session_factory_for

    return session_factory_for(target)()


def main(
    argv: list[str] | None = None,
    *,
    session_for: Callable[[Any], Session] = _session_for,
    copy_database: Callable[..., str | None] = _copy_database,
    deployments: Path = DEPLOYMENTS,
) -> int:
    from app.core.settings import get_settings
    from app.db.session import database_target
    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser(prog="demo_tamper.py")
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--reference", required=True)
    parser.add_argument(
        "--from-db", required=True, help=f"Database to copy; only its {COPY_SUFFIX} is edited."
    )
    parser.add_argument("--fresh-copy", action="store_true", help="Replace an existing copy.")
    parser.add_argument(CONFIRM_FLAG, dest="confirmed", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    source = database_target(settings)
    print(f"Settings point at {source.describe()}.", file=sys.stderr)
    target = copy_name_for(args.from_db)
    if not args.confirmed:
        print(f"Refused: pass {CONFIRM_FLAG} to corrupt data on purpose.", file=sys.stderr)
        return 2
    reason = refusal(
        environment=settings.environment,
        registry_address=str(getattr(settings, "anchor_registry_address", "") or ""),
        target=target,
        dialect=make_url(source.url).get_backend_name(),
        deployments=deployments,
    )
    if reason is None and not _DB_NAME.match(args.from_db):
        reason = f"{args.from_db!r} is not a plain database name."
    if reason is not None:
        print(f"Refused: {reason}", file=sys.stderr)
        return 2
    if target != args.from_db:
        error = copy_database(source, args.from_db, target, fresh=args.fresh_copy)
        if error is not None:
            print(f"Refused: {error}", file=sys.stderr)
            return 2
    print(f"Tampering database {target} only.", file=sys.stderr)
    with session_for(source.with_name(target, setting="tamper copy")) as db:
        try:
            detail = tamper(db, args.scenario, args.reference)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Tampered {target} ({args.scenario}): {detail}")
    print(
        f"Now run scripts/verify_transaction.py --database {target} --transaction-id <id> and "
        f"scripts/audit_anchoring.py --database {target}."
    )
    return 0


if __name__ == "__main__":
    # Run as `python scripts/<name>.py` from anywhere: the repo root holds the app package.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
