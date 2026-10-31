"""Payload builders, one per event type, in schema version 1 (docs/anchoring-design.md section 4).

Source-backed payloads (``ledger_entry``, ``money_audit``, ``transaction_created``) are built from
the source row's columns only, so the full audit can rebuild them and spot an edited row. The rest
are facts whose record is the event log itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import Transaction
from app.services.anchoring.admin_ref import admin_ref
from app.services.anchoring.canonical import (
    hex32,
    money,
    plain_json,
    ratio,
    text_keccak,
    timestamp,
    uuid_text,
)

SCHEMA_VERSION = 1

LEDGER_ENTRY = "ledger_entry"
MONEY_AUDIT = "money_audit"
TRANSACTION_CREATED = "transaction_created"
TRANSACTION_TRANSITION = "transaction_transition"
ADMIN_DECISION = "admin_decision"
BACKFILL_SNAPSHOT = "backfill_snapshot"
BACKFILL_COMPLETED = "backfill_completed"
GLOBAL_EVENT_LINKED = "global_event_linked"

DISPUTE_OPENED = "dispute_opened"
DISPUTE_RETURN_SENT = "dispute_return_sent"
DISPUTE_RETURN_RECEIVED = "dispute_return_received"
DISPUTE_SELLER_RESOLUTION = "dispute_seller_resolution"
DISPUTE_BUYER_RECONFIRMATION = "dispute_buyer_reconfirmation"
DISPUTE_ESCALATED = "dispute_escalated"
DISPUTE_RESOLUTION_CONFIRMED = "dispute_resolution_confirmed"
DISPUTE_AUTO_CLOSED = "dispute_auto_closed"

ACTOR_ADMIN = "admin"


def envelope(
    event_type: str,
    transaction_id: uuid.UUID | str | None,
    at: datetime,
    *,
    backfilled: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "type": event_type,
        "transaction_id": uuid_text(transaction_id),
        "at": timestamp(at),
    }
    if backfilled:
        payload["backfilled"] = True
    return payload


def actor(
    actor_type: str, actor_id: uuid.UUID | None, *, admin_keys: list[str] | None = None
) -> dict[str, Any]:
    """Admins appear only as ``actor_ref`` (decision D5); everyone else by UUID."""
    if actor_type == ACTOR_ADMIN:
        return {
            "actor_type": actor_type,
            "actor_id": None,
            "actor_ref": admin_ref(actor_id, keys=admin_keys),
        }
    return {"actor_type": actor_type, "actor_id": uuid_text(actor_id), "actor_ref": None}


def ledger_entry_payload(
    entry: LedgerEntry, transaction_id: uuid.UUID | None, *, backfilled: bool = False
) -> dict[str, Any]:
    return {
        **envelope(LEDGER_ENTRY, transaction_id, entry.created_at, backfilled=backfilled),
        "ledger_entry_id": uuid_text(entry.id),
        "escrow_id": uuid_text(entry.escrow_id),
        "entry_group_id": uuid_text(entry.entry_group_id),
        "idempotency_key": entry.idempotency_key,
        "account_code": entry.account_code,
        "side": getattr(entry.side, "value", entry.side),
        "amount": money(entry.amount),
        "currency": entry.currency,
        "description": entry.description,
    }


def money_audit_payload(
    event: MoneyAuditEvent, *, backfilled: bool = False, admin_keys: list[str] | None = None
) -> dict[str, Any]:
    return {
        **envelope(MONEY_AUDIT, event.transaction_id, event.occurred_at, backfilled=backfilled),
        "audit_event_id": uuid_text(event.id),
        "action": event.action,
        **actor(event.actor_type, event.actor_id, admin_keys=admin_keys),
        "reason": event.reason,
        "escrow_id": uuid_text(event.escrow_id),
        "attempt_id": uuid_text(event.attempt_id),
        "provider_reference": event.provider_reference,
        "rail_name": event.rail_name,
        "correlation_id": event.correlation_id,
        "amount": money(event.amount),
        "currency": event.currency,
        "details": plain_json(event.details or {}),
    }


def transaction_created_payload(
    transaction: Transaction, *, backfilled: bool = False
) -> dict[str, Any]:
    # custody_provider is left out: it can change after creation (simulated fallback).
    return {
        **envelope(
            TRANSACTION_CREATED, transaction.id, transaction.created_at, backfilled=backfilled
        ),
        "reference": transaction.reference,
        "listing_id": uuid_text(transaction.listing_id),
        "buyer_id": uuid_text(transaction.buyer_id),
        "seller_id": uuid_text(transaction.seller_id),
        "amount": money(transaction.amount),
        "currency": "KES",
    }


def transition_payload(
    transaction_id: uuid.UUID,
    *,
    from_status: str,
    to_status: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    reason: str,
    at: datetime,
    door_rejection_reason: str | None = None,
) -> dict[str, Any]:
    return {
        **envelope(TRANSACTION_TRANSITION, transaction_id, at),
        "from": from_status,
        "to": to_status,
        **actor(actor_type, actor_id),
        "reason": reason,
        "door_rejection_reason": door_rejection_reason,
    }


def dispute_payload(
    event_type: str,
    transaction_id: uuid.UUID,
    dispute_id: uuid.UUID,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    at: datetime,
    facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        **envelope(event_type, transaction_id, at),
        "dispute_id": uuid_text(dispute_id),
        **actor(actor_type, actor_id),
        **facts,
    }


def admin_decision_payload(
    transaction_id: uuid.UUID,
    dispute_id: uuid.UUID,
    *,
    decision: str,
    reason: str,
    admin_id: uuid.UUID | None,
    seller_share: Decimal | None,
    release_amount: Decimal | None,
    refund_amount: Decimal | None,
    at: datetime,
) -> dict[str, Any]:
    return {
        **envelope(ADMIN_DECISION, transaction_id, at),
        "dispute_id": uuid_text(dispute_id),
        "decision": decision,
        "reason": reason,
        "admin_ref": admin_ref(admin_id),
        "seller_share": ratio(seller_share),
        "release_amount": money(release_amount),
        "refund_amount": money(refund_amount),
    }


def global_event_linked_payload(
    transaction_id: uuid.UUID,
    *,
    at: datetime,
    global_leaf_hash: bytes,
    global_audit_event_id: uuid.UUID,
    escrow_id: uuid.UUID,
    provider_reference: str,
) -> dict[str, Any]:
    return {
        **envelope(GLOBAL_EVENT_LINKED, transaction_id, at),
        "global_leaf_hash": hex32(global_leaf_hash),
        "global_audit_event_id": uuid_text(global_audit_event_id),
        "escrow_id": uuid_text(escrow_id),
        "provider_reference": provider_reference,
    }


def free_text(value: str | None) -> str | None:
    """Party-written text is bound by hash, never copied (decision D4)."""
    text = (value or "").strip()
    return text_keccak(text) if text else None


def backfill_snapshot_payload(transaction: Transaction, dispute: Any, *, at: datetime) -> dict:
    """What a transaction looked like when the backfill ran: all it can truthfully attest
    (decision D3; docs/anchoring-design.md 5.9)."""
    snapshot = None
    if dispute is not None:
        snapshot = {
            "dispute_id": uuid_text(dispute.id),
            "status": getattr(dispute.status, "value", dispute.status),
            "admin_decision": getattr(dispute.admin_decision, "value", dispute.admin_decision),
            "admin_reason": dispute.admin_reason,
            "admin_ref": admin_ref(dispute.decided_by_user_id),
            "split_ratio": ratio(dispute.split_ratio),
            "seller_resolution_action": getattr(
                dispute.seller_resolution_action, "value", dispute.seller_resolution_action
            ),
            "resolved_at": timestamp(dispute.resolved_at),
        }
    return {
        **envelope(BACKFILL_SNAPSHOT, transaction.id, at, backfilled=True),
        "status": transaction.status.value,
        "payout_status": transaction.payout_status.value,
        "amount": money(transaction.amount),
        "currency": "KES",
        "dispute": snapshot,
    }


def backfill_completed_payload(*, at: datetime, counts: dict[str, int]) -> dict:
    return {**envelope(BACKFILL_COMPLETED, None, at, backfilled=True), "counts": counts}
