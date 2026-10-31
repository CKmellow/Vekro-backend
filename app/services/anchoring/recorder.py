"""record_anchor_event: the one place anchor events are written (M21 A3).

Each event joins a hash chain: its transaction's, or the global chain for events without one.
``prev_event_hash`` is the chain's latest leaf, read while holding ``SELECT ... FOR UPDATE`` on the
transaction row (an advisory lock for the global chain), so two writers can never claim the same
predecessor (decision D1). The lock lasts until the DB transaction ends. The chain head is cached
on the session in the meantime, so several events in one DB transaction chain in order without
extra flushes.

The event row is written in the caller's DB transaction and never commits by itself. Nothing here
touches the network. When ``anchor_events`` does not exist (partial test schemas), nothing is
recorded, as ``record_money_audit_event`` does for its own table.

A flush guard checks completeness. Every status change, ledger row and money audit row needs its
event. In strict mode (the test suite) a missing one raises. Otherwise it is logged and recorded
as a fallback, so a money operation never fails because of a missed hook (decision D2).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, attributes

from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import Transaction
from app.services.anchoring import payloads
from app.services.anchoring.canonical import ZERO_HASH, canonical_json, leaf_hash, new_salt

_logger = logging.getLogger("app.anchoring")

_TABLE = AnchorEvent.__tablename__
_READY_KEY = "anchor_events_table_ready"
_HEADS_KEY = "anchor_chain_heads"  # chain key -> latest leaf hash in this DB transaction
_SOURCES_KEY = "anchor_sources"  # (source_table, source_id) recorded in this DB transaction
_STATUS_KEY = "anchor_status"  # transaction id -> last recorded status value
_GLOBAL = "global"
# pg_advisory_xact_lock key for the global chain ("vekro-anchor" as an int64).
GLOBAL_CHAIN_LOCK_KEY = 0x76656B726F2D616E

LEDGER_TABLE = LedgerEntry.__tablename__
AUDIT_TABLE = MoneyAuditEvent.__tablename__
TRANSACTIONS_TABLE = Transaction.__tablename__


class AnchorCompletenessError(RuntimeError):
    """A money fact was written without its anchor event (strict mode only)."""


def anchoring_ready(db: Any) -> bool:
    info = getattr(db, "info", None)
    if isinstance(info, dict) and _READY_KEY in info:
        return bool(info[_READY_KEY])
    if not isinstance(db, Session):
        return False
    try:
        # The session's own connection, so the open transaction is left alone.
        ready = inspect(db.connection()).has_table(_TABLE)
    except SQLAlchemyError:
        ready = False
    info[_READY_KEY] = ready
    return ready


def record_anchor_event(
    db: Session,
    *,
    event_type: str,
    transaction_id: uuid.UUID | None,
    payload: dict[str, Any],
    source_table: str | None = None,
    source_id: str | None = None,
) -> AnchorEvent | None:
    if not anchoring_ready(db):
        return None
    payload_text = canonical_json(payload)
    _flush_pending_transaction(db, transaction_id)
    prev = _chain_head(db, transaction_id)
    salt = new_salt()
    leaf = leaf_hash(salt, prev, payload_text)
    anchor = AnchorEvent(
        transaction_id=transaction_id,
        event_type=event_type,
        source_table=source_table,
        source_id=source_id,
        schema_version=payloads.SCHEMA_VERSION,
        payload=payload_text,
        salt=salt,
        prev_event_hash=prev,
        leaf_hash=leaf,
        created_at=datetime.now(UTC),
    )
    db.add(anchor)
    db.info.setdefault(_HEADS_KEY, {})[_chain_key(transaction_id)] = leaf
    if source_table is not None and source_id is not None:
        db.info.setdefault(_SOURCES_KEY, set()).add((source_table, source_id))
    return anchor


def record_ledger_entry(db: Session, entry: LedgerEntry) -> AnchorEvent | None:
    if not anchoring_ready(db):
        return None
    with db.no_autoflush:
        escrow = db.get(Escrow, entry.escrow_id)
    transaction_id = escrow.transaction_id if escrow is not None else None
    return record_anchor_event(
        db,
        event_type=payloads.LEDGER_ENTRY,
        transaction_id=transaction_id,
        payload=payloads.ledger_entry_payload(entry, transaction_id),
        source_table=LEDGER_TABLE,
        source_id=str(entry.id),
    )


def record_money_audit(db: Session, audit: MoneyAuditEvent) -> AnchorEvent | None:
    anchor = record_anchor_event(
        db,
        event_type=payloads.MONEY_AUDIT,
        transaction_id=audit.transaction_id,
        payload=payloads.money_audit_payload(audit),
        source_table=AUDIT_TABLE,
        source_id=str(audit.id),
    )
    if anchor is not None and audit.transaction_id is None:
        _link_global_funding_event(db, audit, anchor)
    return anchor


def record_transaction_created(db: Session, transaction: Transaction) -> AnchorEvent | None:
    # Noted first: the transaction row may be flushed before its event (see below).
    note_status(db, transaction.id, transaction.status.value)
    anchor = record_anchor_event(
        db,
        event_type=payloads.TRANSACTION_CREATED,
        transaction_id=transaction.id,
        payload=payloads.transaction_created_payload(transaction),
        source_table=TRANSACTIONS_TABLE,
        source_id=str(transaction.id),
    )
    return anchor


def note_status(db: Any, transaction_id: uuid.UUID, status_value: str) -> None:
    """Remember the status an event just recorded, for the flush guard."""
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info.setdefault(_STATUS_KEY, {})[transaction_id] = status_value


def _link_global_funding_event(db: Session, audit: MoneyAuditEvent, anchor: AnchorEvent) -> None:
    """Decision D10: a global funding event matched to an escrow is linked into that
    transaction's chain, so its verification includes it."""
    if not audit.provider_reference:
        return
    from app.models.collection_attempt import CollectionAttempt

    try:
        with db.no_autoflush:
            row = db.execute(
                select(Escrow.id, Escrow.transaction_id)
                .join(CollectionAttempt, CollectionAttempt.escrow_id == Escrow.id)
                .where(CollectionAttempt.provider_reference == audit.provider_reference)
                .limit(1)
            ).first()
    except SQLAlchemyError:  # pragma: no cover - collection_attempts missing in a test schema
        return
    if row is None:
        return
    escrow_id, transaction_id = row
    record_anchor_event(
        db,
        event_type=payloads.GLOBAL_EVENT_LINKED,
        transaction_id=transaction_id,
        payload=payloads.global_event_linked_payload(
            transaction_id,
            at=audit.occurred_at,
            global_leaf_hash=anchor.leaf_hash,
            global_audit_event_id=audit.id,
            escrow_id=escrow_id,
            provider_reference=audit.provider_reference,
        ),
    )


def _flush_pending_transaction(db: Session, transaction_id: uuid.UUID | None) -> None:
    """Insert a still-pending transaction row before its event. Without ORM relationships the
    unit of work does not order the two inserts by foreign key, and PostgreSQL enforces it."""
    if transaction_id is None:
        return
    if any(isinstance(obj, Transaction) and obj.id == transaction_id for obj in db.new):
        db.flush()


def _chain_key(transaction_id: uuid.UUID | None) -> str:
    return str(transaction_id) if transaction_id is not None else _GLOBAL


def _chain_head(db: Session, transaction_id: uuid.UUID | None) -> bytes:
    heads = db.info.setdefault(_HEADS_KEY, {})
    key = _chain_key(transaction_id)
    if key in heads:
        return heads[key]
    with db.no_autoflush:
        if transaction_id is not None:
            db.execute(
                select(Transaction.id).where(Transaction.id == transaction_id).with_for_update()
            )
            condition = AnchorEvent.transaction_id == transaction_id
        else:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": GLOBAL_CHAIN_LOCK_KEY}
                )
            condition = AnchorEvent.transaction_id.is_(None)
        last = db.execute(
            select(AnchorEvent.leaf_hash).where(condition).order_by(AnchorEvent.id.desc()).limit(1)
        ).scalar_one_or_none()
    head = bytes(last) if last is not None else ZERO_HASH
    heads[key] = head
    return head


def _strict() -> bool:
    from app.core.settings import get_settings

    return bool(get_settings().anchor_strict_events)


def _missing(db: Session, message: str) -> None:
    if _strict():
        raise AnchorCompletenessError(message)
    _logger.error("anchor_event_missing %s; recording a fallback event", message)


@event.listens_for(Session, "before_flush")
def _guard_completeness(db: Session, _flush_context, _instances) -> None:
    if not anchoring_ready(db):
        return
    sources = db.info.get(_SOURCES_KEY, set())
    statuses = db.info.get(_STATUS_KEY, {})
    for obj in list(db.new):
        if isinstance(obj, LedgerEntry) and (LEDGER_TABLE, str(obj.id)) not in sources:
            _missing(db, f"ledger entry {obj.id} has no anchor event")
            record_ledger_entry(db, obj)
        elif isinstance(obj, MoneyAuditEvent) and (AUDIT_TABLE, str(obj.id)) not in sources:
            _missing(db, f"money audit event {obj.id} has no anchor event")
            obj.occurred_at = obj.occurred_at or datetime.now(UTC)
            record_money_audit(db, obj)
        elif isinstance(obj, Transaction) and obj.id not in statuses:
            _missing(db, f"transaction {obj.id} was created without transaction_created")
            obj.created_at = obj.created_at or datetime.now(UTC)
            record_transaction_created(db, obj)
    for obj in list(db.dirty):
        if not isinstance(obj, Transaction):
            continue
        history = attributes.get_history(obj, "status")
        if not history.has_changes():
            continue
        current = getattr(obj.status, "value", obj.status)
        if statuses.get(obj.id) == current:
            continue
        previous = history.deleted[0] if history.deleted else None
        _missing(db, f"transaction {obj.id} moved to {current} without a transition event")
        from app.services.transitions import record_transition

        record_transition(
            db,
            obj.id,
            from_status=getattr(previous, "value", previous) or "unknown",
            to_status=current,
            actor_type="unknown",
            actor_id=None,
            reason="unrecorded_transition",
        )


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_soft_rollback")
def _reset_transaction_state(db: Session, *_args) -> None:
    # Locks and chain heads end with the DB transaction; the table check stays valid.
    for key in (_HEADS_KEY, _SOURCES_KEY, _STATUS_KEY):
        db.info.pop(key, None)
