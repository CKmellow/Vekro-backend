"""One-off backfill of anchor events for history written before M21 (decision D3).

Events are created for every ledger row, money audit row and transaction that has none yet, in
original time order and marked ``"backfilled": true``. Then one ``backfill_snapshot`` per
transaction records its state at backfill time. Status transitions and dispute events from before
M21 were never recorded anywhere trustworthy, so none are invented. History before the backfill is
attested only from the moment of the backfill, not from when it happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import Transaction
from app.services.anchoring import payloads
from app.services.anchoring.recorder import (
    AUDIT_TABLE,
    LEDGER_TABLE,
    TRANSACTIONS_TABLE,
    anchoring_ready,
    record_anchor_event,
)
from app.services.dispute_summary import latest_dispute

COMMIT_EVERY = 500


class BackfillRefusedError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackfillResult:
    transactions_created: int
    ledger_entries: int
    money_audit_events: int
    snapshots: int


def backfill_anchor_events(
    db: Session, *, resume: bool = False, now: datetime | None = None
) -> BackfillResult:
    if not anchoring_ready(db):
        raise BackfillRefusedError("anchor_events does not exist; run the migrations first.")
    done = db.execute(
        select(AnchorEvent.id).where(AnchorEvent.event_type == payloads.BACKFILL_COMPLETED)
    ).first()
    if done is not None and not resume:
        raise BackfillRefusedError("The backfill already ran. Pass --resume to fill new gaps.")

    at = now or datetime.now(UTC)
    anchored = {
        (table, source_id)
        for table, source_id in db.execute(
            select(AnchorEvent.source_table, AnchorEvent.source_id).where(
                AnchorEvent.source_table.is_not(None)
            )
        ).all()
    }
    escrow_transactions = dict(db.execute(select(Escrow.id, Escrow.transaction_id)).all())

    # (timestamp, order within a moment, id, record) so each chain follows the original order.
    pending: list[tuple[datetime, int, str, Any]] = []
    for transaction in db.execute(select(Transaction)).scalars():
        if (TRANSACTIONS_TABLE, str(transaction.id)) not in anchored:
            pending.append((_utc(transaction.created_at), 0, str(transaction.id), transaction))
    for entry in db.execute(select(LedgerEntry)).scalars():
        if (LEDGER_TABLE, str(entry.id)) not in anchored:
            pending.append((_utc(entry.created_at), 1, str(entry.id), entry))
    for audit in db.execute(select(MoneyAuditEvent)).scalars():
        if (AUDIT_TABLE, str(audit.id)) not in anchored:
            pending.append((_utc(audit.occurred_at), 2, str(audit.id), audit))
    pending.sort(key=lambda item: item[:3])

    counts = {"transactions_created": 0, "ledger_entries": 0, "money_audit_events": 0}
    for written, (_at, _order, source_id, record) in enumerate(pending, start=1):
        if isinstance(record, Transaction):
            _record(
                db,
                payloads.TRANSACTION_CREATED,
                record.id,
                payloads.transaction_created_payload(record, backfilled=True),
                TRANSACTIONS_TABLE,
                source_id,
            )
            counts["transactions_created"] += 1
        elif isinstance(record, LedgerEntry):
            transaction_id = escrow_transactions.get(record.escrow_id)
            _record(
                db,
                payloads.LEDGER_ENTRY,
                transaction_id,
                payloads.ledger_entry_payload(record, transaction_id, backfilled=True),
                LEDGER_TABLE,
                source_id,
            )
            counts["ledger_entries"] += 1
        else:
            _record(
                db,
                payloads.MONEY_AUDIT,
                record.transaction_id,
                payloads.money_audit_payload(record, backfilled=True),
                AUDIT_TABLE,
                source_id,
            )
            counts["money_audit_events"] += 1
        if written % COMMIT_EVERY == 0:
            db.commit()

    snapshots = 0
    for transaction in db.execute(select(Transaction).order_by(Transaction.created_at)).scalars():
        _record(
            db,
            payloads.BACKFILL_SNAPSHOT,
            transaction.id,
            payloads.backfill_snapshot_payload(
                transaction, latest_dispute(db, transaction.id), at=at
            ),
        )
        snapshots += 1
        if snapshots % COMMIT_EVERY == 0:
            db.commit()

    record_anchor_event(
        db,
        event_type=payloads.BACKFILL_COMPLETED,
        transaction_id=None,
        payload=payloads.backfill_completed_payload(
            at=at, counts={**counts, "snapshots": snapshots}
        ),
    )
    db.commit()
    return BackfillResult(snapshots=snapshots, **counts)


def _record(db, event_type, transaction_id, payload, source_table=None, source_id=None) -> None:
    record_anchor_event(
        db,
        event_type=event_type,
        transaction_id=transaction_id,
        payload=payload,
        source_table=source_table,
        source_id=source_id,
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
