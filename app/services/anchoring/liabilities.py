"""Proof of liabilities (M24 B2, #261; docs/anchoring.md "Proof of liabilities").

A snapshot puts every open escrow (held = funded - released - refunded > 0) into a Merkle sum tree
(``sum_tree.py``) and anchors its root and total in the global chain as ``liabilities_snapshot``.
The buyer of each held order can fetch their leaf, salt and proof and check, in the browser, that
their amount is counted in the anchored total. The payload names no order and no buyer.

What this proves and what it does not is in docs/anchoring.md: it fixes the total Vekro owes, it
does not show that the money exists (that is the reserves side).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.liabilities import LiabilitiesLeaf, LiabilitiesSnapshot
from app.services.anchoring import payloads
from app.services.anchoring.canonical import hex32, new_salt
from app.services.anchoring.recorder import record_anchor_event
from app.services.anchoring.sum_tree import (
    SumNode,
    SumTree,
    amount_cents,
    leaf,
    order_commit,
)
from app.services.anchoring.verification import BatchTrees, event_view


def build_liabilities_snapshot(db: Session, *, now: datetime | None = None) -> LiabilitiesSnapshot:
    """Snapshot every open escrow, anchor the root and total, commit. Safe to run repeatedly."""
    at = now or datetime.now(UTC)
    held_rows = db.execute(
        select(
            Escrow.transaction_id,
            Escrow.funded_amount - Escrow.released_amount - Escrow.refunded_amount,
        )
        .where(Escrow.funded_amount > Escrow.released_amount + Escrow.refunded_amount)
        .order_by(Escrow.transaction_id)
    ).all()

    leaves: list[tuple[uuid.UUID, bytes, bytes, int, SumNode]] = []
    for transaction_id, held in held_rows:
        salt, commit, cents = new_salt(), order_commit(transaction_id), amount_cents(held)
        leaves.append((transaction_id, salt, commit, cents, leaf(salt, commit, cents)))
    root = SumTree([row[4] for row in leaves]).root if leaves else None

    snapshot = LiabilitiesSnapshot(
        created_at=at,
        root_hash=root.hash if root else None,
        total_cents=Decimal(root.sum if root else 0),
        leaf_count=len(leaves),
    )
    db.add(snapshot)
    db.flush()
    for index, (transaction_id, salt, commit, cents, _) in enumerate(leaves):
        db.add(
            LiabilitiesLeaf(
                snapshot_id=snapshot.id,
                leaf_index=index,
                transaction_id=transaction_id,
                salt=salt,
                order_commit=commit,
                amount_cents=Decimal(cents),
            )
        )
    anchor = record_anchor_event(
        db,
        event_type=payloads.LIABILITIES_SNAPSHOT,
        transaction_id=None,
        payload=payloads.liabilities_snapshot_payload(
            snapshot.id,
            at=at,
            root=snapshot.root_hash,
            total_cents=int(snapshot.total_cents),
            leaf_count=snapshot.leaf_count,
        ),
    )
    if anchor is not None:
        db.flush()
        snapshot.anchor_event_id = anchor.id
    db.commit()
    return snapshot


def liabilities_proof(db: Session, transaction_id: uuid.UUID) -> dict[str, Any] | None:
    """The order's inclusion proof in the latest snapshot that holds it, or None.

    Only the latest snapshot overall counts: an order that is no longer held (released or
    refunded) has no proof, even if an older snapshot included it.
    """
    latest_id = db.execute(select(func.max(LiabilitiesSnapshot.id))).scalar()
    if latest_id is None:
        return None
    snapshot = db.get(LiabilitiesSnapshot, latest_id)
    mine = db.execute(
        select(LiabilitiesLeaf).where(
            LiabilitiesLeaf.snapshot_id == latest_id,
            LiabilitiesLeaf.transaction_id == transaction_id,
        )
    ).scalar()
    if snapshot is None or mine is None or snapshot.root_hash is None:
        return None
    rows = db.execute(
        select(LiabilitiesLeaf)
        .where(LiabilitiesLeaf.snapshot_id == latest_id)
        .order_by(LiabilitiesLeaf.leaf_index)
    ).scalars()
    tree = SumTree([leaf(r.salt, r.order_commit, int(r.amount_cents)) for r in rows])
    anchor = db.get(AnchorEvent, snapshot.anchor_event_id) if snapshot.anchor_event_id else None
    return {
        "snapshot_id": snapshot.id,
        "created_at": snapshot.created_at,
        "root": {"hash": hex32(snapshot.root_hash), "sum": str(int(snapshot.total_cents))},
        "total": payloads.money(Decimal(int(snapshot.total_cents)) / 100),
        "leaf_count": snapshot.leaf_count,
        "leaf": {
            "salt": hex32(mine.salt),
            "order_commit": hex32(mine.order_commit),
            "amount_cents": str(int(mine.amount_cents)),
        },
        "proof": [
            {
                "sibling_hash": hex32(step.sibling.hash),
                "sibling_sum": str(step.sibling.sum),
                "side": step.side,
            }
            for step in tree.proof(mine.leaf_index)
        ],
        "anchor": event_view(anchor, BatchTrees(db)) if anchor is not None else None,
    }
