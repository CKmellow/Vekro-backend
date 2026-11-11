"""A seller's verified track record (M21 A9, issue #231).

Every completed sale (released, or released by a support decision) records a
``seller_sale_completed`` event in the global chain. Its payload names the seller, the outcome
and an amount band, under an unlinkable ``sale_ref``: no buyer, no order id. The public track
record lists those events with their Merkle proofs. Anyone can check each one against the
AnchorRegistry and count the seller's sales without trusting Vekro. A sale that is not anchored
yet is listed as pending.

The proof shows sales really happened through escrow; it cannot prove that no refunds were left
out, so the record speaks only of completed sales.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.anchor_event import AnchorEvent
from app.models.store_event import StoreEvent
from app.models.transaction import Transaction
from app.services.anchoring.chain_config import SUPPORTED_CHAINS
from app.services.anchoring.recorder import (
    SELLER_SALES_SOURCE,
    STORE_EVENTS_TABLE,
    record_seller_sale,
)
from app.services.anchoring.verification import BatchTrees, event_view
from app.services.transitions import SALE_STATUSES

MAX_SALES = 200


def seller_track_record(db: Session, seller_id: uuid.UUID, settings: Any) -> dict[str, Any]:
    sale_ids = [
        str(item)
        for item in db.execute(
            select(Transaction.id).where(
                Transaction.seller_id == seller_id, Transaction.status.in_(SALE_STATUSES)
            )
        ).scalars()
    ]
    events = (
        list(
            db.execute(
                select(AnchorEvent)
                .where(
                    AnchorEvent.source_table == SELLER_SALES_SOURCE,
                    AnchorEvent.source_id.in_(sale_ids),
                )
                .order_by(AnchorEvent.id.desc())
                .limit(MAX_SALES)
            ).scalars()
        )
        if sale_ids
        else []
    )
    trees = BatchTrees(db)
    sales = [event_view(event, trees) for event in events]
    store_event_ids = [
        str(item)
        for item in db.execute(
            select(StoreEvent.id).where(StoreEvent.user_id == seller_id)
        ).scalars()
    ]
    history = (
        list(
            db.execute(
                select(AnchorEvent)
                .where(
                    AnchorEvent.source_table == STORE_EVENTS_TABLE,
                    AnchorEvent.source_id.in_(store_event_ids),
                )
                .order_by(AnchorEvent.id.asc())
            ).scalars()
        )
        if store_event_ids
        else []
    )
    return {
        "seller_id": str(seller_id),
        "chain": {
            "chain_id": settings.anchor_chain_id,
            "network": SUPPORTED_CHAINS.get(settings.anchor_chain_id),
            "registry_address": settings.anchor_registry_address or None,
        },
        "completed_sales": len(sale_ids),
        "recorded_sales": len(events),
        "sales": sales,
        "store_history": [event_view(event, trees) for event in history],
        "server_time": datetime.now(UTC),
    }


def backfill_seller_sales(db: Session) -> int:
    """Record track-record events for completed sales from before A9. Idempotent."""
    recorded = 0
    for transaction in db.execute(
        select(Transaction).where(Transaction.status.in_(SALE_STATUSES))
    ).scalars():
        at = transaction.released_at or transaction.updated_at or datetime.now(UTC)
        if record_seller_sale(
            db, transaction, outcome=transaction.status.value, at=at, backfilled=True
        ):
            recorded += 1
    db.commit()
    return recorded
