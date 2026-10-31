"""PostgreSQL-only checks for the anchor event log (M21 A3), against a database migrated to head.

Skipped unless TEST_POSTGRES_URL points at one; CI's Migrations job sets it. Locally:

    docker run -d --name vekro-pg -e POSTGRES_USER=vekro -e POSTGRES_PASSWORD=vekro \\
        -e POSTGRES_DB=vekro -p 55432:5432 postgres:16
    DATABASE_URL=postgresql+psycopg://vekro:vekro@127.0.0.1:55432/vekro alembic upgrade head
    TEST_POSTGRES_URL=postgresql+psycopg://vekro:vekro@127.0.0.1:55432/vekro pytest -m postgres

Rows written here cannot be deleted (that is the point), so every run uses fresh ids.
"""

import os
import threading
import time
import uuid

import pytest
from app.models.anchor_event import AnchorEvent
from app.services.anchoring.recorder import record_anchor_event, record_transaction_created
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from tests.test_simulated_custody_full_flow import _build_listing, _build_user

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL unset"),
]


@pytest.fixture(scope="module")
def sessions():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"], pool_size=12)
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def _transaction(factory) -> uuid.UUID:
    from app.models.transaction import Transaction, TransactionStatus
    from app.models.user import UserRole

    suffix = f"{uuid.uuid4().int % 10**4:04d}"
    buyer = _build_user(role=UserRole.BUYER, suffix=suffix)
    buyer.phone = f"+2547{uuid.uuid4().int % 10**8:08d}"
    seller = _build_user(role=UserRole.SELLER, suffix=suffix)
    seller.phone = f"+2547{uuid.uuid4().int % 10**8:08d}"
    listing = _build_listing(seller_id=seller.id, serialized=False)
    listing.unique_id = None
    with factory() as db:
        transaction = Transaction(
            id=uuid.uuid4(),
            listing_id=listing.id,
            buyer_id=buyer.id,
            seller_id=seller.id,
            amount=listing.price,
            status=TransactionStatus.AWAITING_PAYMENT,
        )
        # No ORM relationships here, so insert in foreign-key order by hand.
        db.add_all([buyer, seller])
        db.flush()
        db.add(listing)
        db.flush()
        db.add(transaction)
        record_transaction_created(db, transaction)
        db.commit()
        return transaction.id


def _record(db: Session, transaction_id: uuid.UUID, label: str) -> AnchorEvent:
    event = record_anchor_event(
        db,
        event_type="probe",
        transaction_id=transaction_id,
        payload={"v": 1, "type": "probe", "label": label},
    )
    assert event is not None
    return event


def _chain(factory, transaction_id: uuid.UUID) -> list[AnchorEvent]:
    with factory() as db:
        return list(
            db.execute(
                select(AnchorEvent)
                .where(AnchorEvent.transaction_id == transaction_id)
                .order_by(AnchorEvent.id)
            ).scalars()
        )


def _assert_linear(events: list[AnchorEvent]) -> None:
    prev = bytes(32)
    for event in events:
        assert bytes(event.prev_event_hash) == prev
        prev = bytes(event.leaf_hash)


def test_a_second_writer_waits_for_the_first_and_chains_after_it(sessions) -> None:
    transaction_id = _transaction(sessions)
    first_locked = threading.Event()
    first_committed_at: list[float] = []
    second_recorded_at: list[float] = []

    def first() -> None:
        with sessions() as db:
            _record(db, transaction_id, "first")
            first_locked.set()
            time.sleep(0.5)  # hold the row lock while the second writer tries
            db.commit()
            first_committed_at.append(time.monotonic())

    def second() -> None:
        first_locked.wait(5)
        with sessions() as db:
            _record(db, transaction_id, "second")  # blocks on SELECT ... FOR UPDATE
            second_recorded_at.append(time.monotonic())
            db.commit()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert second_recorded_at[0] >= first_committed_at[0]
    events = _chain(sessions, transaction_id)
    assert [e.event_type for e in events] == ["transaction_created", "probe", "probe"]
    _assert_linear(events)


def test_many_concurrent_writers_never_fork_a_chain(sessions) -> None:
    transaction_id = _transaction(sessions)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            barrier.wait(5)
            for round_ in range(5):
                with sessions() as db:
                    _record(db, transaction_id, f"{index}-{round_}")
                    db.commit()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    events = _chain(sessions, transaction_id)
    assert len(events) == 1 + 8 * 5
    _assert_linear(events)
    assert len({bytes(e.prev_event_hash) for e in events}) == len(events)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE anchor_events SET payload = '{}' WHERE id = :id",
        "DELETE FROM anchor_events WHERE id = :id",
        "UPDATE anchor_events SET seq = :seq, batch_id = 1, leaf_index = 0, payload = '{}' "
        "WHERE id = :id",
        "TRUNCATE anchor_events",
        "TRUNCATE ledger_entries",
        "TRUNCATE money_audit_events",
    ],
)
def test_the_append_only_triggers_refuse_edits(sessions, statement) -> None:
    transaction_id = _transaction(sessions)
    event_id = _chain(sessions, transaction_id)[0].id
    with sessions() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(statement), {"id": event_id, "seq": -event_id})
        db.rollback()


def test_the_batcher_assignment_is_allowed_exactly_once(sessions) -> None:
    transaction_id = _transaction(sessions)
    event_id = _chain(sessions, transaction_id)[0].id
    assign = "UPDATE anchor_events SET seq = :seq, batch_id = :seq, leaf_index = 0 WHERE id = :id"
    with sessions() as db:
        db.execute(text(assign), {"id": event_id, "seq": -event_id})
        db.commit()
    with sessions() as db:
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(assign), {"id": event_id, "seq": -event_id - 1})
        db.rollback()


def test_a_real_order_flow_anchors_cleanly_on_postgres(sessions, monkeypatch) -> None:
    """The service paths on real foreign keys, triggers and row locks: create, pay (simulated
    callback funding), dispatch, arrive, accept, and the executor's payout posting."""
    import json

    from app.models.user import UserRole
    from app.schemas.transaction import CreateTransactionRequest
    from app.services import transaction as transaction_service
    from app.services.escrow_service import run_payout_executor_once
    from app.services.transaction import (
        confirm_buyer_delivery_otp,
        confirm_payment_callback,
        create_transaction,
        dispatch_transaction,
        mark_transaction_arrived,
    )

    from tests.test_simulated_custody_full_flow import _build_callback_payload

    suffix = f"{uuid.uuid4().int % 10**4:04d}"
    buyer = _build_user(role=UserRole.BUYER, suffix=suffix)
    buyer.phone = f"+2547{uuid.uuid4().int % 10**8:08d}"
    seller = _build_user(role=UserRole.SELLER, suffix=suffix)
    seller.phone = f"+2547{uuid.uuid4().int % 10**8:08d}"
    listing = _build_listing(seller_id=seller.id, serialized=False)
    listing.unique_id = None
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "111222")

    with sessions() as db:
        db.add_all([buyer, seller])
        db.flush()
        db.add(listing)
        db.commit()
        transaction = create_transaction(db, buyer, CreateTransactionRequest(listing_id=listing.id))
        confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
        dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
        mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
        confirm_buyer_delivery_otp(
            db, transaction_id=transaction.id, buyer=buyer, otp_code="111222"
        )
        run_payout_executor_once(db)
        status = transaction.status.value

    events = _chain(sessions, transaction.id)
    _assert_linear(events)
    types = [event.event_type for event in events]
    assert types[0] == "transaction_created"
    assert types.count("ledger_entry") == 4  # callback funding + executor release, 2 rows each
    last = next(
        json.loads(e.payload) for e in reversed(events) if e.event_type == "transaction_transition"
    )
    assert last["to"] == status == "released"
