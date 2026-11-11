"""M21 A5: the batcher and submitter against an in-memory registry with the contract's rules.

Covers gapless batching, write-ahead submission, confirmations, the on-chain idempotency check,
same-nonce fee bumps, reverted transactions, reorgs, divergence halts, an unreachable RPC, startup
validation and the admin status endpoint. tests/test_anchoring_chain.py repeats the key paths on a
real Anvil under the ``chain`` marker.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from app.core.settings import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.anchor_batch import AnchorBatch, AnchorBatchStatus
from app.models.anchor_event import AnchorEvent
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import TransactionStatus
from app.models.user import PartyRole, UserRole
from app.services.anchoring.batcher import AnchoringConfig, build_next_batch, run_once
from app.services.anchoring.chain import AnchorCall
from app.services.anchoring.merkle import merkle_root
from app.services.anchoring.status import anchoring_status
from app.services.transaction import confirm_payment_callback
from app.services.transitions import transition_transaction
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.anchoring_fakes import FakeRegistry
from tests.test_money_audit import _authenticated_client
from tests.test_return_case_timeouts import _db_with_audit
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _seed_transaction_context,
)

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CONFIG = AnchoringConfig(
    max_batch_size=1000,
    min_confirmations=2,
    fee_bump_after=timedelta(minutes=2),
    max_fee_per_gas=50 * 10**9,
    retry_backoff=timedelta(minutes=1),
)


def _db() -> Session:
    db = _db_with_audit()
    Base.metadata.create_all(db.get_bind(), tables=[cast(Table, AnchorBatch.__table__)])
    return db


def _record_events(db: Session, suffix: str) -> None:
    """A paid order: transaction_created plus the payment transition, both anchored."""
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))


def _events(db: Session) -> list[AnchorEvent]:
    return list(db.execute(select(AnchorEvent).order_by(AnchorEvent.id)).scalars())


def _batch(db: Session, batch_id: int) -> AnchorBatch:
    batch = db.get(AnchorBatch, batch_id)
    assert batch is not None
    db.refresh(batch)
    return batch


def _anchor_to_confirmation(db: Session, chain: FakeRegistry, now: datetime) -> AnchorBatch:
    result = run_once(db, chain, CONFIG, now=now)
    assert result.action == "submitted"
    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=now).action == "confirmed"
    assert result.batch_id is not None
    return _batch(db, result.batch_id)


def test_building_assigns_gapless_seq_and_fixes_the_root() -> None:
    db = _db()
    _record_events(db, "a1")
    events = _events(db)
    assert len(events) >= 2 and all(event.seq is None for event in events)

    batch = build_next_batch(db, chain_id=31337, registry_address="0x" + "11" * 20, max_size=1000)
    assert batch is not None
    assert (batch.id, batch.first_seq, batch.last_seq) == (1, 1, len(events))
    assert [(e.seq, e.batch_id, e.leaf_index) for e in _events(db)] == [
        (i + 1, 1, i) for i in range(len(events))
    ]
    assert batch.root == merkle_root([bytes(e.leaf_hash) for e in events])
    assert batch.prev_root == bytes(32)
    # One batch in flight at a time.
    _record_events(db, "a2")
    assert build_next_batch(db, chain_id=31337, registry_address="0x", max_size=1000) is None


def test_a_batch_is_submitted_write_ahead_then_confirmed_after_enough_blocks() -> None:
    db = _db()
    _record_events(db, "b1")
    chain = FakeRegistry()
    sent_with_record: list[bool] = []
    original_send = chain.send_raw

    def send_after_checking_the_record(raw: bytes) -> None:
        stored = db.execute(select(AnchorBatch)).scalar_one()
        sent_with_record.append(stored.raw_tx == raw and stored.tx_hash is not None)
        original_send(raw)

    chain.send_raw = send_after_checking_the_record  # type: ignore[method-assign]

    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    assert sent_with_record == [True]
    chain.mine(1)
    waiting = run_once(db, chain, CONFIG, now=T0)
    assert (waiting.action, waiting.detail) == ("waiting", "1 confirmation(s)")
    chain.mine(1)
    assert run_once(db, chain, CONFIG, now=T0).action == "confirmed"

    batch = _batch(db, 1)
    on_chain = chain.get_batch(1)
    assert batch.status == AnchorBatchStatus.CONFIRMED and on_chain is not None
    assert on_chain.root == batch.root and batch.block_number == on_chain.block_number
    assert batch.block_hash is not None and batch.raw_tx is None
    assert run_once(db, chain, CONFIG, now=T0).action == "idle"


def test_the_next_batch_continues_the_chain_and_the_seq_range() -> None:
    db = _db()
    chain = FakeRegistry()
    _record_events(db, "c1")
    first = _anchor_to_confirmation(db, chain, T0)
    _record_events(db, "c2")
    second = _anchor_to_confirmation(db, chain, T0)
    assert second.id == 2
    assert second.prev_root == first.root
    assert second.first_seq == first.last_seq + 1
    assert chain.state.last_seq == second.last_seq


def test_a_batch_already_on_chain_is_confirmed_without_a_new_transaction() -> None:
    db = _db()
    _record_events(db, "d1")
    batch = build_next_batch(db, chain_id=31337, registry_address="0x", max_size=1000)
    assert batch is not None
    chain = FakeRegistry()
    # Anchored earlier, but the database lost track (for example a restore from backup).
    call = AnchorCall(
        batch.id, batch.root, batch.prev_root, batch.leaf_count, batch.first_seq, batch.last_seq
    )
    assert chain.anchor_directly(call)
    chain.mine(2)

    assert run_once(db, chain, CONFIG, now=T0).action == "confirmed"
    assert chain.broadcasts == []


def test_a_crash_between_the_record_and_the_broadcast_rebroadcasts_the_same_transaction() -> None:
    db = _db()
    _record_events(db, "e1")
    chain = FakeRegistry()
    original_send = chain.send_raw

    def crash(_raw: bytes) -> None:
        raise KeyboardInterrupt("worker killed before the broadcast")

    chain.send_raw = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        run_once(db, chain, CONFIG, now=T0)
    recorded = _batch(db, 1)
    assert recorded.status == AnchorBatchStatus.SUBMITTED and recorded.raw_tx is not None
    tx_hash = recorded.tx_hash

    chain.send_raw = original_send  # type: ignore[method-assign]
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(seconds=10)).action == "waiting"
    assert chain.broadcasts == [tx_hash]
    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(seconds=20)).action == "confirmed"
    assert _batch(db, 1).tx_hashes == tx_hash.hex()


def test_a_stuck_transaction_is_replaced_at_the_same_nonce_with_higher_fees() -> None:
    db = _db()
    _record_events(db, "f1")
    chain = FakeRegistry()
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    first = _batch(db, 1)
    first_fee, first_nonce = first.max_fee_per_gas, first.nonce
    assert first_fee is not None

    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=1)).action == "waiting"
    bumped = run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=3))
    assert bumped.action == "fee_bumped"
    batch = _batch(db, 1)
    assert batch.nonce == first_nonce
    assert batch.max_fee_per_gas is not None and batch.max_fee_per_gas > first_fee
    assert len(batch.tx_hashes.split(",")) == 2

    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=4)).action == "confirmed"


def test_fees_never_exceed_the_configured_cap() -> None:
    db = _db()
    _record_events(db, "f2")
    chain = FakeRegistry(base_fee=100 * 10**9)
    run_once(db, chain, CONFIG, now=T0)
    batch = _batch(db, 1)
    assert batch.max_fee_per_gas == CONFIG.max_fee_per_gas
    assert batch.max_priority_fee_per_gas is not None
    assert batch.max_priority_fee_per_gas <= CONFIG.max_fee_per_gas


def test_a_reverted_transaction_is_retried_after_the_backoff() -> None:
    db = _db()
    _record_events(db, "g1")
    chain = FakeRegistry()
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    # The registry refuses the queued call when it is mined (simulated by giving it a prev root
    # the registry does not hold), so the transaction reverts.
    queued = next(iter(chain.state.mempool.values()))
    queued.call = AnchorCall(*(queued.call.__dict__ | {"prev_root": b"\x01" * 32}).values())
    chain.mine(1)

    retry = run_once(db, chain, CONFIG, now=T0)
    assert retry.action == "retry_scheduled"
    assert _batch(db, 1).status == AnchorBatchStatus.FAILED_RETRYABLE
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(seconds=30)).detail == "retry backoff"
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=2)).action == "submitted"
    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=3)).action == "confirmed"


def test_a_reorg_that_drops_a_confirmed_batch_resubmits_the_same_values() -> None:
    db = _db()
    _record_events(db, "h1")
    chain = FakeRegistry()
    before = chain.snapshot()
    confirmed = _anchor_to_confirmation(db, chain, T0)
    root = confirmed.root

    chain.revert(before)  # the blocks holding the anchor are gone
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=1)).action == "submitted"
    batch = _batch(db, 1)
    assert batch.root == root and batch.status == AnchorBatchStatus.SUBMITTED
    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0 + timedelta(minutes=2)).action == "confirmed"
    on_chain = chain.get_batch(1)
    assert on_chain is not None and on_chain.root == root


def test_a_different_root_on_chain_halts_anchoring_with_an_audit_event() -> None:
    db = _db()
    _record_events(db, "i1")
    chain = FakeRegistry()
    batch = build_next_batch(db, chain_id=31337, registry_address="0x", max_size=1000)
    assert batch is not None
    forged = AnchorCall(1, b"\x07" * 32, bytes(32), batch.leaf_count, 1, batch.last_seq)
    assert chain.anchor_directly(forged)

    halted = run_once(db, chain, CONFIG, now=T0)
    assert halted.action == "halted"
    assert "different root" in (halted.detail or "")
    assert _batch(db, 1).status == AnchorBatchStatus.HALTED
    audit = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "anchoring_halted")
    ).scalar_one()
    assert audit.details["batch_id"] == 1

    _record_events(db, "i2")
    assert run_once(db, chain, CONFIG, now=T0).action == "halted"
    assert db.execute(select(AnchorBatch)).scalars().all() == [_batch(db, 1)]


def test_an_unreachable_rpc_leaves_escrows_untouched_and_events_waiting() -> None:
    db = _db()
    chain = FakeRegistry()
    chain.down = True
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="j1"
    )

    unavailable = run_once(db, chain, CONFIG, now=T0)
    assert unavailable.action == "chain_unavailable"
    assert "unreachable" in (_batch(db, 1).last_error or "")

    # Money flows never touch the chain: payment and dispatch work while it is down.
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    transition_transaction(
        db, transaction, TransactionStatus.OUT_FOR_DELIVERY, actor_type="user", reason="test"
    )
    db.commit()
    assert transaction.status == TransactionStatus.OUT_FOR_DELIVERY

    chain.down = False
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0).action == "confirmed"
    # Events recorded during the outage go into the next batch.
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "secret_key": "test-secret",
        "database_url": "sqlite://",
        "alembic_database_url": "sqlite://",
    }
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_enabling_anchoring_requires_the_chain_settings_and_never_echoes_the_key() -> None:
    with pytest.raises(ValueError) as error:
        _settings(anchor_enabled=True, anchor_private_key="not-a-key-but-secret-looking")
    message = str(error.value)
    for name in ("ANCHOR_RPC_URL", "ANCHOR_REGISTRY_ADDRESS", "ANCHOR_PRIVATE_KEY"):
        assert name in message
    assert "ANCHOR_ADMIN_REF_SECRET" in message
    assert "not-a-key-but-secret-looking" not in message


def test_production_refuses_the_local_anvil_chain() -> None:
    with pytest.raises(ValueError, match="31337"):
        _settings(
            environment="production",
            anchor_enabled=True,
            anchor_rpc_url="http://localhost:8545",
            anchor_chain_id=31337,
            anchor_registry_address="0x" + "ab" * 20,
            anchor_private_key="0x" + "12" * 32,
            anchor_admin_ref_secret="x" * 40,
        )


def test_a_valid_configuration_hides_the_key_from_every_repr() -> None:
    key = "0x" + "4f" * 32
    settings = _settings(
        anchor_enabled=True,
        anchor_rpc_url="http://localhost:8545",
        anchor_registry_address="0x" + "ab" * 20,
        anchor_private_key=key,
        anchor_admin_ref_secret="y" * 40,
    )
    assert key not in repr(settings) and key not in str(settings.model_dump())


@pytest.fixture
def status_db() -> Iterator[Session]:
    # StaticPool: the TestClient thread must see the same in-memory database.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[cast(Table, AnchorBatch.__table__), cast(Table, AnchorEvent.__table__)]
    )
    db = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


def _stored_batch(batch_id: int, status: AnchorBatchStatus, **extra: object) -> AnchorBatch:
    return AnchorBatch(
        id=batch_id,
        root=bytes([batch_id]) * 32,
        prev_root=bytes([batch_id - 1]) * 32 if batch_id > 1 else bytes(32),
        first_seq=batch_id * 10 - 9,
        last_seq=batch_id * 10,
        leaf_count=10,
        status=status,
        chain_id=31337,
        registry_address="0x" + "11" * 20,
        tx_hashes="",
        attempts=1,
        **extra,
    )


def test_admin_status_shows_progress_but_never_the_key(
    status_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_db.add(_stored_batch(1, AnchorBatchStatus.CONFIRMED, confirmed_at=T0, block_number=7))
    status_db.add(_stored_batch(2, AnchorBatchStatus.SUBMITTED, tx_hash=b"\x09" * 32))
    status_db.commit()

    key = "0x" + "59" * 32
    monkeypatch.setenv("ANCHOR_PRIVATE_KEY", key)
    from app.core.settings import get_settings

    get_settings.cache_clear()
    try:
        client = _authenticated_client(monkeypatch, UserRole.ADMIN)
        response = client.get("/admin/anchoring/status")
        seller = _authenticated_client(monkeypatch, PartyRole.SELLER)
        assert seller.get("/admin/anchoring/status").status_code == 403
    finally:
        get_settings.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["last_confirmed"]["batch_id"] == 1
    assert body["last_confirmed"]["block_number"] == 7
    assert body["in_flight"]["batch_id"] == 2
    assert body["in_flight"]["status"] == "submitted"
    assert body["in_flight"]["tx_hash"] == "0x" + "09" * 32
    assert body["confirmed_batches"] == 1
    assert body["halted"] is False
    assert body["anchorer_address"].startswith("0x")
    assert key[2:] not in response.text


def test_status_counts_unanchored_events() -> None:
    db = _db()
    _record_events(db, "l1")
    status = anchoring_status(db, _settings())
    assert status.unanchored_events == len(_events(db))
    assert status.oldest_unanchored_at is not None
    assert status.in_flight is None and status.last_confirmed is None
