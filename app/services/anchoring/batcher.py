"""Batch committed anchor events and anchor them on the AnchorRegistry (M21 A5).

One worker at a time (a PostgreSQL session advisory lock) and one batch in flight. Each run:

1. Stops if a batch is HALTED: the chain disagreed with the database and a person must look.
2. Re-checks the latest confirmed batches against the registry. A batch that a reorg dropped goes
   back to BUILT and is resubmitted with the same values; a different root on-chain halts.
3. Advances the in-flight batch, or builds the next one from unbatched events. Building assigns the
   gapless ``seq`` (decision D1) and fixes the batch's root for good.

Submitting is write-ahead: the signed transaction and its hash are committed before the broadcast,
so a crash in between only means the next run rebroadcasts the same transaction. Before every new
submission the registry is asked whether the batch is already there (idempotency). A transaction
that sits unmined past ``fee_bump_after`` is replaced at the same nonce with higher fees.

Nothing here runs inside a money operation. An unreachable RPC leaves every escrow untouched; the
events wait and are anchored on a later run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.anchor_batch import IN_FLIGHT_STATUSES, AnchorBatch, AnchorBatchStatus
from app.models.anchor_event import AnchorEvent
from app.services.anchoring.canonical import ZERO_HASH, hex32
from app.services.anchoring.chain import AnchorCall, ChainUnavailableError, RegistryClient
from app.services.anchoring.merkle import merkle_root
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event

_logger = logging.getLogger("app.anchoring.batcher")

# pg_try_advisory_lock key for the batcher ("vekro-batcher" truncated to 8 bytes).
BATCHER_LOCK_KEY = 0x76656B726F2D6274
RECHECK_CONFIRMED_BATCHES = 3
GWEI = 10**9


@dataclass(frozen=True)
class AnchoringConfig:
    max_batch_size: int = 1000
    min_confirmations: int = 2
    fee_bump_after: timedelta = timedelta(seconds=120)
    max_fee_per_gas: int = 5 * GWEI
    retry_backoff: timedelta = timedelta(seconds=60)

    @classmethod
    def from_settings(cls, settings: Any) -> AnchoringConfig:
        return cls(
            max_batch_size=settings.anchor_max_batch_size,
            min_confirmations=settings.anchor_min_confirmations,
            fee_bump_after=timedelta(seconds=settings.anchor_fee_bump_after_seconds),
            max_fee_per_gas=int(Decimal(settings.anchor_max_fee_per_gas_gwei) * GWEI),
            retry_backoff=timedelta(seconds=settings.anchor_retry_backoff_seconds),
        )


@dataclass(frozen=True)
class RunResult:
    action: str
    batch_id: int | None = None
    detail: str | None = None


class AnchoringHaltedError(RuntimeError):
    pass


def run_once(
    db: Session,
    client: RegistryClient,
    config: AnchoringConfig,
    *,
    now: datetime | None = None,
) -> RunResult:
    lock = _BatcherLock(db)
    if not lock.acquire():
        return RunResult("locked", detail="another batcher is running")
    try:
        return _run(db, client, config, now or datetime.now(UTC))
    finally:
        lock.release()


def _run(db: Session, client: RegistryClient, config: AnchoringConfig, now: datetime) -> RunResult:
    halted = _first_with_status(db, (AnchorBatchStatus.HALTED,))
    if halted is not None:
        return RunResult("halted", halted.id, halted.halt_reason)

    batch: AnchorBatch | None = None
    try:
        _recheck_confirmed(db, client, now)
        batch = _first_with_status(db, IN_FLIGHT_STATUSES)
        if batch is None:
            batch = build_next_batch(
                db,
                chain_id=client.chain_id,
                registry_address=client.registry_address,
                max_size=config.max_batch_size,
                now=now,
            )
            if batch is None:
                return RunResult("idle")
        return _advance(db, client, config, batch, now)
    except AnchoringHaltedError as exc:
        return RunResult("halted", batch.id if batch is not None else None, str(exc))
    except ChainUnavailableError as exc:
        db.rollback()
        if batch is not None:
            batch.last_error = f"chain unavailable: {exc}"[:500]
            db.commit()
        _logger.warning("anchoring_chain_unavailable error=%s", exc)
        return RunResult("chain_unavailable", batch.id if batch is not None else None, str(exc))


def build_next_batch(
    db: Session,
    *,
    chain_id: int,
    registry_address: str,
    max_size: int,
    now: datetime | None = None,
) -> AnchorBatch | None:
    """Give the oldest unbatched events their gapless seq and fix the next batch's root."""
    if _first_with_status(db, (*IN_FLIGHT_STATUSES, AnchorBatchStatus.HALTED)) is not None:
        return None
    query = select(AnchorEvent).where(AnchorEvent.seq.is_(None)).order_by(AnchorEvent.id)
    query = query.limit(max_size)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    events = list(db.execute(query).scalars())
    if not events:
        return None

    last = db.execute(select(AnchorBatch).order_by(AnchorBatch.id.desc()).limit(1)).scalar()
    batch_id = (last.id if last else 0) + 1
    first_seq = (last.last_seq if last else 0) + 1
    for index, event in enumerate(events):
        event.seq = first_seq + index
        event.batch_id = batch_id
        event.leaf_index = index
    batch = AnchorBatch(
        id=batch_id,
        root=merkle_root([event.leaf_hash for event in events]),
        prev_root=last.root if last else ZERO_HASH,
        first_seq=first_seq,
        last_seq=first_seq + len(events) - 1,
        leaf_count=len(events),
        status=AnchorBatchStatus.BUILT,
        chain_id=chain_id,
        registry_address=registry_address,
        tx_hashes="",
        attempts=0,
        created_at=now or datetime.now(UTC),
    )
    db.add(batch)
    db.commit()
    _logger.info(
        "anchoring_batch_built batch_id=%s first_seq=%s last_seq=%s root=%s",
        batch.id,
        batch.first_seq,
        batch.last_seq,
        hex32(batch.root),
    )
    return batch


def _advance(
    db: Session,
    client: RegistryClient,
    config: AnchoringConfig,
    batch: AnchorBatch,
    now: datetime,
) -> RunResult:
    if batch.status in (AnchorBatchStatus.BUILT, AnchorBatchStatus.FAILED_RETRYABLE):
        if batch.next_attempt_at is not None and _aware(batch.next_attempt_at) > now:
            return RunResult("waiting", batch.id, "retry backoff")
        on_chain = client.root_of(batch.id)
        if on_chain == batch.root:
            # Already anchored (a crash after broadcast, or a rebuilt state): just confirm it.
            batch.status = AnchorBatchStatus.SUBMITTED
            batch.submitted_at = batch.submitted_at or now
            db.commit()
            return _check_submitted(db, client, config, batch, now)
        if on_chain != ZERO_HASH:
            _halt(db, batch, f"registry holds a different root for batch {batch.id}", now)
        expected_count = batch.id - 1
        count = client.batch_count()
        if count != expected_count:
            reason = f"registry has {count} batches; expected {expected_count} before this"
            _halt(db, batch, reason, now)
        if batch.id > 1 and client.root_of(batch.id - 1) != batch.prev_root:
            _halt(db, batch, f"registry root of batch {batch.id - 1} is not this batch's prev", now)
        return _submit(db, client, config, batch, now, nonce=client.nonce(pending=True))
    return _check_submitted(db, client, config, batch, now)


def _submit(
    db: Session,
    client: RegistryClient,
    config: AnchoringConfig,
    batch: AnchorBatch,
    now: datetime,
    *,
    nonce: int,
    min_fees: tuple[int, int] | None = None,
) -> RunResult:
    max_fee, priority = client.fees()
    if min_fees is not None:
        max_fee = max(max_fee, min_fees[0])
        priority = max(priority, min_fees[1])
    max_fee = min(max_fee, config.max_fee_per_gas)
    priority = min(priority, max_fee)
    signed = client.sign_anchor(
        AnchorCall(
            batch_id=batch.id,
            root=batch.root,
            prev_root=batch.prev_root,
            leaf_count=batch.leaf_count,
            first_seq=batch.first_seq,
            last_seq=batch.last_seq,
        ),
        nonce=nonce,
        max_fee=max_fee,
        priority_fee=priority,
    )
    replacing = batch.status == AnchorBatchStatus.SUBMITTED
    batch.tx_hash = signed.tx_hash
    batch.raw_tx = signed.raw
    batch.nonce = nonce
    batch.max_fee_per_gas = max_fee
    batch.max_priority_fee_per_gas = priority
    batch.tx_hashes = _append_hash(batch.tx_hashes, signed.tx_hash)
    batch.attempts = (batch.attempts or 0) + 1
    batch.status = AnchorBatchStatus.SUBMITTED
    batch.submitted_at = now
    batch.next_attempt_at = None
    batch.last_error = None
    # Write-ahead: the transaction is on record before it can reach the chain.
    db.commit()
    client.send_raw(signed.raw)
    _logger.info(
        "anchoring_batch_submitted batch_id=%s tx=%s nonce=%s max_fee=%s replacing=%s",
        batch.id,
        signed.tx_hash.hex(),
        nonce,
        max_fee,
        replacing,
    )
    action = "fee_bumped" if replacing else "submitted"
    return RunResult(action, batch.id, "0x" + signed.tx_hash.hex())


def _check_submitted(
    db: Session,
    client: RegistryClient,
    config: AnchoringConfig,
    batch: AnchorBatch,
    now: datetime,
) -> RunResult:
    on_chain = client.root_of(batch.id)
    if on_chain not in (ZERO_HASH, batch.root):
        _halt(db, batch, f"registry holds a different root for batch {batch.id}", now)
    if on_chain == batch.root:
        stored = client.get_batch(batch.id)
        if stored is None:  # pragma: no cover - root_of said it exists
            return RunResult("waiting", batch.id, "batch not readable yet")
        confirmations = client.block_number() - stored.block_number + 1
        batch.block_number = stored.block_number
        if confirmations < config.min_confirmations:
            db.commit()
            return RunResult("waiting", batch.id, f"{confirmations} confirmation(s)")
        batch.status = AnchorBatchStatus.CONFIRMED
        batch.block_hash = client.block_hash(stored.block_number)
        batch.confirmed_at = now
        batch.raw_tx = None
        batch.last_error = None
        db.commit()
        _logger.info(
            "anchoring_batch_confirmed batch_id=%s block=%s", batch.id, stored.block_number
        )
        return RunResult("confirmed", batch.id)

    receipt = None
    for tx_hash in _hashes(batch):
        receipt = client.receipt(tx_hash)
        if receipt is not None:
            break
    if receipt is not None and not receipt.succeeded:
        return _retry_later(db, config, batch, now, "anchor transaction reverted")
    if receipt is not None:
        return RunResult("waiting", batch.id, "mined; registry not updated yet")

    if batch.nonce is not None and client.nonce(pending=False) > batch.nonce:
        return _retry_later(db, config, batch, now, "nonce used by another transaction")
    if batch.raw_tx is None:
        # Nothing to rebroadcast (only after a confirmed batch was dropped by a reorg).
        return _submit(db, client, config, batch, now, nonce=client.nonce(pending=True))
    submitted_at = _aware(batch.submitted_at) if batch.submitted_at else now
    if now - submitted_at >= config.fee_bump_after and batch.nonce is not None:
        bumped = (
            (batch.max_fee_per_gas or 0) * 9 // 8 + 1,
            (batch.max_priority_fee_per_gas or 0) * 9 // 8 + 1,
        )
        if (batch.max_fee_per_gas or 0) < config.max_fee_per_gas:
            return _submit(db, client, config, batch, now, nonce=batch.nonce, min_fees=bumped)
    # Rebroadcast what is on record: harmless when it is already in the mempool, and it repairs a
    # crash between the write-ahead commit and the broadcast.
    client.send_raw(batch.raw_tx)
    return RunResult("waiting", batch.id, "not mined yet")


def _recheck_confirmed(db: Session, client: RegistryClient, now: datetime) -> None:
    recent = list(
        db.execute(
            select(AnchorBatch)
            .where(AnchorBatch.status == AnchorBatchStatus.CONFIRMED)
            .order_by(AnchorBatch.id.desc())
            .limit(RECHECK_CONFIRMED_BATCHES)
        ).scalars()
    )
    for batch in sorted(recent, key=lambda item: item.id):
        on_chain = client.root_of(batch.id)
        if on_chain == batch.root:
            continue
        if on_chain != ZERO_HASH:
            _halt(db, batch, f"registry root for confirmed batch {batch.id} changed", now)
        _logger.warning("anchoring_batch_reorged batch_id=%s", batch.id)
        batch.status = AnchorBatchStatus.BUILT
        batch.confirmed_at = None
        batch.block_number = None
        batch.block_hash = None
        batch.raw_tx = None
        batch.last_error = "dropped by a chain reorganisation; resubmitting"
        db.commit()
        # Later confirmed batches can only be on-chain if this one is; they are rechecked too.


def _retry_later(
    db: Session, config: AnchoringConfig, batch: AnchorBatch, now: datetime, reason: str
) -> RunResult:
    batch.status = AnchorBatchStatus.FAILED_RETRYABLE
    batch.last_error = reason
    batch.raw_tx = None
    batch.next_attempt_at = now + config.retry_backoff
    db.commit()
    _logger.warning("anchoring_batch_retry batch_id=%s reason=%s", batch.id, reason)
    return RunResult("retry_scheduled", batch.id, reason)


def _halt(db: Session, batch: AnchorBatch, reason: str, now: datetime) -> None:
    db.rollback()
    batch.status = AnchorBatchStatus.HALTED
    batch.halt_reason = reason[:500]
    record_money_audit_event(
        db,
        action="anchoring_halted",
        actor_type=ACTOR_SYSTEM,
        reason=reason[:255],
        details={"batch_id": batch.id, "root": hex32(batch.root)},
        occurred_at=now,
    )
    db.commit()
    _logger.critical("anchoring_halted batch_id=%s reason=%s", batch.id, reason)
    raise AnchoringHaltedError(reason)


def _first_with_status(db: Session, statuses: tuple[AnchorBatchStatus, ...]) -> AnchorBatch | None:
    return db.execute(
        select(AnchorBatch)
        .where(AnchorBatch.status.in_(statuses))
        .order_by(AnchorBatch.id)
        .limit(1)
    ).scalar()


def _append_hash(existing: str, tx_hash: bytes) -> str:
    hashes = [item for item in (existing or "").split(",") if item]
    hashes.append(tx_hash.hex())
    return ",".join(hashes)


def _hashes(batch: AnchorBatch) -> list[bytes]:
    return [bytes.fromhex(item) for item in reversed((batch.tx_hashes or "").split(",")) if item]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def unbatched_summary(db: Session) -> tuple[int, datetime | None]:
    count, oldest = db.execute(
        select(func.count(AnchorEvent.id), func.min(AnchorEvent.created_at)).where(
            AnchorEvent.seq.is_(None)
        )
    ).one()
    return int(count or 0), (_aware(oldest) if oldest else None)


class _BatcherLock:
    """A session-level advisory lock on its own connection, held across the run's commits."""

    def __init__(self, db: Session) -> None:
        self._engine = db.get_bind()
        self._conn: Any = None

    def acquire(self) -> bool:
        if self._engine.dialect.name != "postgresql":
            return True
        self._conn = self._engine.connect()
        got = self._conn.execute(select(func.pg_try_advisory_lock(BATCHER_LOCK_KEY))).scalar()
        if not got:
            self._conn.close()
            self._conn = None
        return bool(got)

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(select(func.pg_advisory_unlock(BATCHER_LOCK_KEY)))
        finally:
            self._conn.close()
            self._conn = None
