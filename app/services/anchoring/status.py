"""What the admin anchoring status shows (M21 A5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.models.anchor_batch import IN_FLIGHT_STATUSES, AnchorBatch, AnchorBatchStatus
from app.models.collection_attempt import AttemptOutcome
from app.models.payout_attempt import PayoutAttempt
from app.schemas.anchoring import (
    AnchorBatchSummary,
    AnchoringStatusResponse,
    LiabilitiesStatus,
    ReservesStatus,
)
from app.services.anchoring.batcher import unbatched_summary
from app.services.anchoring.canonical import hex32
from app.services.anchoring.chain_config import SUPPORTED_CHAINS, secret_value
from app.services.anchoring.payout_gate import GATE_CODES, HALTED


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
    waiting, waiting_since, halted_payouts = _gated_payouts(db)
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
        payouts_gated=settings.anchor_gate_payouts,
        payouts_waiting_for_anchor=waiting,
        oldest_payout_waiting_since=waiting_since,
        payouts_halted=halted_payouts,
        liabilities=_latest_liabilities(db),
        reserves=(reserves := _latest_reserves(db)),
        reserves_shortfall=any(r.status == "shortfall" for r in reserves),
        server_time=datetime.now(UTC),
    )


def _latest_liabilities(db: Session) -> LiabilitiesStatus | None:
    from app.models.liabilities import LiabilitiesSnapshot

    if not inspect(db.get_bind()).has_table(LiabilitiesSnapshot.__tablename__):
        return None  # a schema from before M24 B2 (older test databases)
    snapshot = db.execute(
        select(LiabilitiesSnapshot).order_by(LiabilitiesSnapshot.id.desc()).limit(1)
    ).scalar()
    if snapshot is None:
        return None
    return LiabilitiesStatus(
        snapshot_id=snapshot.id,
        total=f"{Decimal(int(snapshot.total_cents)) / 100:.2f}",
        leaf_count=snapshot.leaf_count,
        at=snapshot.created_at,
        anchored=snapshot.anchor_event_id is not None,
    )


def _latest_reserves(db: Session) -> list[ReservesStatus]:
    from app.models.anchor_event import AnchorEvent
    from app.services.anchoring.payloads import RESERVES_RECONCILIATION

    events = db.execute(
        select(AnchorEvent)
        .where(AnchorEvent.event_type == RESERVES_RECONCILIATION)
        .order_by(AnchorEvent.id.desc())
        .limit(50)
    ).scalars()
    latest: dict[str, ReservesStatus] = {}
    for event in events:
        body = json.loads(event.payload)
        if body["provider"] not in latest:
            latest[body["provider"]] = ReservesStatus(
                provider=body["provider"],
                status=body["status"],
                method=body["method"],
                liability_total=body["liability_total"],
                attested_total=body["attested_total"],
                difference=body["difference"],
                at=body["at"],
            )
    return sorted(latest.values(), key=lambda r: r.provider)


def _gated_payouts(db: Session) -> tuple[int, datetime | None, int]:
    if not inspect(db.connection()).has_table(PayoutAttempt.__tablename__):
        return 0, None, 0
    open_attempts = (
        select(PayoutAttempt.failure_code, func.count(), func.min(PayoutAttempt.created_at))
        .where(
            PayoutAttempt.outcome == AttemptOutcome.UNKNOWN,
            PayoutAttempt.failure_code.in_(GATE_CODES),
        )
        .group_by(PayoutAttempt.failure_code)
    )
    waiting, since, halted = 0, None, 0
    for code, count, oldest in db.execute(open_attempts).tuples():
        if code == HALTED:
            halted += int(count)
            continue
        waiting += int(count)
        if oldest is not None:
            oldest = oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC)
            since = oldest if since is None else min(since, oldest)
    return waiting, since, halted
