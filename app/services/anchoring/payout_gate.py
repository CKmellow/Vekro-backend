"""Anchor before payout (M21 A8, issue #230).

With ``ANCHOR_GATE_PAYOUTS=true`` no payout or refund reaches a rail until the decision that
authorised it is public and immutable. That decision is the ``payout_<kind>_queued`` audit row,
written in the same DB transaction as the status change. Before each new payout request the
executor checks, in this order:

1. The queued event exists, its leaf still recomputes, and its payload names this attempt, its
   transaction and its amount. The destination phone must match the anchored ``destination_ref``
   (an HMAC, so phones never appear in party-visible payloads).
2. The event is in a CONFIRMED batch.
3. Its Merkle proof leads to the root the AnchorRegistry holds for that batch, read from the
   chain and not from the database.

When the event is not anchored yet, or the chain cannot be read, the payout waits and is retried
on the next run. When anything disagrees, payouts for that attempt HALT with an audit event, and
a person has to look. Someone who can edit the database therefore cannot mark an order released,
or redirect a payout, without the change failing against a public commitment made earlier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.anchor_batch import AnchorBatch, AnchorBatchStatus
from app.models.anchor_event import AnchorEvent
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payout_attempt import PayoutAttempt
from app.services.anchoring.admin_ref import configured_keys
from app.services.anchoring.canonical import ZERO_HASH, leaf_hash, money
from app.services.anchoring.chain import ChainUnavailableError
from app.services.anchoring.merkle import MerkleTree, root_from_proof
from app.services.anchoring.recorder import AUDIT_TABLE, anchoring_ready

WAITING = "waiting_for_anchor"
UNAVAILABLE = "anchor_check_unavailable"
HALTED = "anchor_mismatch_halted"
GATE_CODES = (WAITING, UNAVAILABLE, HALTED)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: str | None = None
    detail: str | None = None


ALLOW = GateDecision(True)


def destination_ref(phone: str | None, *, keys: list[str] | None = None) -> str | None:
    """HMAC of the payout destination, bound into the anchored authorisation."""
    normalized = (phone or "").strip()
    if not normalized:
        return None
    key = (keys or configured_keys())[0]
    message = ("payout-destination:" + normalized).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _destination_matches(ref: str, phone: str) -> bool:
    return any(
        hmac.compare_digest(destination_ref(phone, keys=[key]) or "", ref)
        for key in configured_keys()
    )


def check_payout_gate(
    db: Session,
    attempt: PayoutAttempt,
    *,
    transaction_id: str,
    destination_phone: str,
    root_of: Callable[[int], bytes],
) -> GateDecision:
    if not anchoring_ready(db):
        return GateDecision(False, UNAVAILABLE, "The anchor log is not available.")

    audit = db.execute(
        select(MoneyAuditEvent)
        .where(
            MoneyAuditEvent.attempt_id == attempt.id,
            MoneyAuditEvent.action.like("payout\\_%\\_queued", escape="\\"),
        )
        .order_by(MoneyAuditEvent.occurred_at)
        .limit(1)
    ).scalar()
    if audit is None:
        return GateDecision(False, HALTED, "The payout has no recorded authorisation.")
    event = db.execute(
        select(AnchorEvent).where(
            AnchorEvent.source_table == AUDIT_TABLE, AnchorEvent.source_id == str(audit.id)
        )
    ).scalar()
    if event is None:
        return GateDecision(False, HALTED, "The payout's authorisation was never anchored.")

    leaf = leaf_hash(bytes(event.salt), bytes(event.prev_event_hash), event.payload)
    if leaf != bytes(event.leaf_hash):
        return GateDecision(False, HALTED, "The authorisation event no longer hashes correctly.")
    body = json.loads(event.payload)
    if (
        body.get("attempt_id") != str(attempt.id)
        or body.get("transaction_id") != transaction_id
        or body.get("amount") != money(attempt.amount)
    ):
        return GateDecision(False, HALTED, "The payout differs from its anchored authorisation.")
    ref = (body.get("details") or {}).get("destination_ref")
    if ref and not _destination_matches(ref, destination_phone):
        return GateDecision(False, HALTED, "The payout destination differs from the anchored one.")

    if event.batch_id is None or event.leaf_index is None:
        return GateDecision(False, WAITING, "The authorisation is not in a batch yet.")
    batch = db.get(AnchorBatch, int(event.batch_id))
    if batch is None or batch.status != AnchorBatchStatus.CONFIRMED:
        return GateDecision(False, WAITING, "The authorisation's batch is not confirmed yet.")

    leaves = [
        bytes(item)
        for item in db.execute(
            select(AnchorEvent.leaf_hash)
            .where(AnchorEvent.batch_id == batch.id)
            .order_by(AnchorEvent.leaf_index)
        ).scalars()
    ]
    index = int(event.leaf_index)
    if len(leaves) != batch.leaf_count or index >= len(leaves) or leaves[index] != leaf:
        return GateDecision(False, HALTED, "The authorisation's batch has been altered.")
    computed = root_from_proof(leaf, MerkleTree(leaves).proof(index))
    try:
        on_chain = root_of(batch.id)
    except ChainUnavailableError as exc:
        return GateDecision(False, UNAVAILABLE, f"The registry cannot be read: {exc}"[:255])
    if on_chain == ZERO_HASH:
        # Dropped by a reorg: the batcher anchors it again.
        return GateDecision(False, WAITING, "The authorisation's batch is not on-chain.")
    if on_chain != computed:
        return GateDecision(False, HALTED, "The authorisation does not match the anchored root.")
    return ALLOW
