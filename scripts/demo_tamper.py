#!/usr/bin/env python3
"""Tamper demo for the anchoring defence (M21 A6). CORRUPTS DATA ON PURPOSE.

    .venv/bin/python scripts/demo_tamper.py <scenario> --reference VK-XXXXXX \\
        --i-understand-this-corrupts-data

Scenarios, each what an insider with database access might try:

- edit-ledger:         raise the amount on the order's first ledger row
- delete-middle-event: delete an event from the middle of the order's chain
- delete-last-event:   delete the order's latest event (only the full audit can tell)
- change-status:       set the order's status without recording a transition

The append-only triggers are switched off around the edit, as a table owner could do
(PostgreSQL: ALTER TABLE ... DISABLE TRIGGER USER; SQLite: drop and recreate). Afterwards run
scripts/verify_transaction.py and scripts/audit_anchoring.py to watch them catch it.

Refuses to run with ENVIRONMENT=production, and without the confirmation flag.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

SCENARIOS = ("edit-ledger", "delete-middle-event", "delete-last-event", "change-status")
CONFIRM_FLAG = "--i-understand-this-corrupts-data"


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
        new_amount = entry.amount * 10 + Decimal("1.00")
        with triggers_off(db, "ledger_entries"):
            db.execute(
                update(LedgerEntry).where(LedgerEntry.id == entry.id).values(amount=new_amount)
            )
        detail = f"ledger row {entry.id}: {entry.amount} -> {new_amount}"
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
        target = (
            TransactionStatus.RELEASED
            if transaction.status != TransactionStatus.RELEASED
            else TransactionStatus.REFUNDED_BUYER
        )
        # A raw UPDATE: the ORM flush guard would otherwise record a transition.
        db.execute(
            update(Transaction).where(Transaction.id == transaction.id).values(status=target)
        )
        detail = f"status {transaction.status.value} -> {target.value} with no transition"
    else:  # pragma: no cover - argparse limits the choices
        raise ValueError(scenario)
    db.commit()
    return detail


def _session() -> Session:
    from app.db.session import _get_session_factory

    return _get_session_factory()()


def main(argv: list[str] | None = None, *, session: Callable[[], Session] = _session) -> int:
    from app.core.settings import get_settings

    parser = argparse.ArgumentParser(prog="demo_tamper.py")
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--reference", required=True)
    parser.add_argument(CONFIRM_FLAG, dest="confirmed", action="store_true")
    args = parser.parse_args(argv)

    if get_settings().environment.strip().lower() == "production":
        print("Refused: this demo never runs in production.", file=sys.stderr)
        return 2
    if not args.confirmed:
        print(f"Refused: pass {CONFIRM_FLAG} to corrupt data on purpose.", file=sys.stderr)
        return 2
    with session() as db:
        try:
            detail = tamper(db, args.scenario, args.reference)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Tampered ({args.scenario}): {detail}")
    print("Now run scripts/verify_transaction.py and scripts/audit_anchoring.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
