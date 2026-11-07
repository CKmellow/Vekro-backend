"""What the admin anchoring status shows (M21 A5)."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.anchor_batch import IN_FLIGHT_STATUSES, AnchorBatch, AnchorBatchStatus
from app.schemas.anchoring import AnchorBatchSummary, AnchoringStatusResponse
from app.services.anchoring.batcher import unbatched_summary
from app.services.anchoring.canonical import hex32
from app.services.anchoring.chain_config import SUPPORTED_CHAINS, secret_value


@lru_cache(maxsize=4)
def anchorer_address(private_key: str) -> str | None:
    if not private_key:
        return None
    try:
        from eth_account import Account

        return str(Account.from_key(private_key).address)
    except Exception:  # a malformed key is reported by startup validation, never echoed here
        return None


def batch_summary(batch: AnchorBatch | None) -> AnchorBatchSummary | None:
    if batch is None:
        return None
    return AnchorBatchSummary(
        batch_id=batch.id,
        status=batch.status.value,
        root=hex32(batch.root),
        first_seq=batch.first_seq,
        last_seq=batch.last_seq,
        leaf_count=batch.leaf_count,
        tx_hash=("0x" + batch.tx_hash.hex()) if batch.tx_hash else None,
        attempts=batch.attempts or 0,
        last_error=batch.last_error,
        block_number=batch.block_number,
        submitted_at=batch.submitted_at,
        confirmed_at=batch.confirmed_at,
    )


def anchoring_status(db: Session, settings: Any) -> AnchoringStatusResponse:
    def first(*statuses: AnchorBatchStatus, newest: bool = False) -> AnchorBatch | None:
        order = AnchorBatch.id.desc() if newest else AnchorBatch.id.asc()
        return db.execute(
            select(AnchorBatch).where(AnchorBatch.status.in_(statuses)).order_by(order).limit(1)
        ).scalar()

    halted = first(AnchorBatchStatus.HALTED)
    confirmed_count = db.execute(
        select(func.count(AnchorBatch.id)).where(AnchorBatch.status == AnchorBatchStatus.CONFIRMED)
    ).scalar_one()
    unanchored, oldest = unbatched_summary(db)
    return AnchoringStatusResponse(
        enabled=settings.anchor_enabled,
        chain_id=settings.anchor_chain_id,
        network=SUPPORTED_CHAINS.get(settings.anchor_chain_id),
        registry_address=settings.anchor_registry_address or None,
        anchorer_address=anchorer_address(secret_value(settings.anchor_private_key)),
        halted=halted is not None,
        halt_reason=halted.halt_reason if halted else None,
        last_confirmed=batch_summary(first(AnchorBatchStatus.CONFIRMED, newest=True)),
        in_flight=batch_summary(halted or first(*IN_FLIGHT_STATUSES)),
        confirmed_batches=int(confirmed_count),
        unanchored_events=unanchored,
        oldest_unanchored_at=oldest,
        server_time=datetime.now(UTC),
    )
