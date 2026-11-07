"""The verification bundle for one transaction (M21 A6): data only, never a verdict.

Everything a party needs to check their order's history without trusting Vekro: each event's
canonical payload, salt and hashes in chain order, its batch, leaf index and Merkle proof, and the
live values (status, amount) to compare with the last anchored facts. The verifier reads each
batch root from the AnchorRegistry itself (scripts/verify_transaction.py, the web app's
"Verified record" panel); the bundle's batch roots are only hints.

Global events linked into the chain (decision D10) come along in ``linked_events`` so their proofs
can be checked too.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.anchor_batch import AnchorBatch
from app.models.anchor_event import AnchorEvent
from app.models.transaction import Transaction
from app.services.anchoring import payloads
from app.services.anchoring.canonical import hex32
from app.services.anchoring.chain_config import SUPPORTED_CHAINS
from app.services.anchoring.merkle import MerkleTree

BUNDLE_VERSION = 1


def build_transaction_verification(
    db: Session, transaction: Transaction, settings: Any
) -> dict[str, Any]:
    events = list(
        db.execute(
            select(AnchorEvent)
            .where(AnchorEvent.transaction_id == transaction.id)
            .order_by(AnchorEvent.id)
        ).scalars()
    )
    linked_hashes = [
        json.loads(event.payload)["global_leaf_hash"]
        for event in events
        if event.event_type == payloads.GLOBAL_EVENT_LINKED
    ]
    linked = _global_events_by_leaf(db, linked_hashes)
    trees = BatchTrees(db)
    return {
        "version": BUNDLE_VERSION,
        "transaction_id": str(transaction.id),
        "reference": transaction.reference,
        "chain": {
            "chain_id": settings.anchor_chain_id,
            "network": SUPPORTED_CHAINS.get(settings.anchor_chain_id),
            "registry_address": settings.anchor_registry_address or None,
        },
        "current": {
            "status": transaction.status.value,
            "amount": payloads.money(transaction.amount),
        },
        "events": [event_view(event, trees) for event in events],
        "linked_events": [event_view(event, trees) for event in linked],
        "server_time": datetime.now(UTC),
    }


def _global_events_by_leaf(db: Session, leaf_hashes: list[str]) -> list[AnchorEvent]:
    wanted = {bytes.fromhex(item.removeprefix("0x")) for item in leaf_hashes}
    if not wanted:
        return []
    rows = db.execute(
        select(AnchorEvent)
        .where(AnchorEvent.transaction_id.is_(None), AnchorEvent.leaf_hash.in_(wanted))
        .order_by(AnchorEvent.id)
    ).scalars()
    return list(rows)


def event_view(event: AnchorEvent, trees: BatchTrees) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "payload": event.payload,
        "salt": hex32(bytes(event.salt)),
        "prev_event_hash": hex32(bytes(event.prev_event_hash)),
        "leaf_hash": hex32(bytes(event.leaf_hash)),
        "created_at": event.created_at,
        "anchor": trees.anchor_for(event),
    }


class BatchTrees:
    """Builds each batch's Merkle tree once per request."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._trees: dict[int, tuple[AnchorBatch, MerkleTree | None] | None] = {}

    def anchor_for(self, event: AnchorEvent) -> dict[str, Any] | None:
        if event.batch_id is None or event.leaf_index is None or event.seq is None:
            return None
        loaded = self._load(int(event.batch_id))
        if loaded is None:
            return None
        batch, tree = loaded
        index = int(event.leaf_index)
        # A batch that lost events cannot give a proof; the verifier reports the mismatch.
        proof = tree.proof(index) if tree is not None and index < len(tree) else []
        return {
            "batch_id": batch.id,
            "seq": int(event.seq),
            "leaf_index": int(event.leaf_index),
            "leaf_count": batch.leaf_count,
            "batch_root": hex32(batch.root),
            "batch_status": batch.status.value,
            "tx_hash": ("0x" + batch.tx_hash.hex()) if batch.tx_hash else None,
            "block_number": batch.block_number,
            "proof": [{"sibling": hex32(step.sibling), "side": step.side} for step in proof],
        }

    def _load(self, batch_id: int) -> tuple[AnchorBatch, MerkleTree | None] | None:
        if batch_id not in self._trees:
            batch = self._db.get(AnchorBatch, batch_id)
            if batch is None:
                self._trees[batch_id] = None
            else:
                leaves = [
                    bytes(leaf)
                    for leaf in self._db.execute(
                        select(AnchorEvent.leaf_hash)
                        .where(AnchorEvent.batch_id == batch_id)
                        .order_by(AnchorEvent.leaf_index)
                    ).scalars()
                ]
                self._trees[batch_id] = (batch, MerkleTree(leaves) if leaves else None)
        return self._trees[batch_id]


def find_transaction(db: Session, transaction_id: uuid.UUID) -> Transaction | None:
    return db.get(Transaction, transaction_id)
