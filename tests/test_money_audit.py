import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.session import get_db
from app.main import app
from app.models.escrow import Escrow
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from app.models.user_session import UserSession
from app.services.audit import (
    ACTOR_ADMIN,
    ACTOR_SYSTEM,
    build_correlation_id,
    record_money_audit_event,
)
from app.services.auth import ActiveSessionContext
from app.services.escrow_service import EscrowService, run_payout_executor_once
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.test_escrow_service_orchestration import (
    _build_db_session,
    _build_user,
    _seed_transaction,
)

_AUDIT_TABLE_DDL = """
CREATE TABLE money_audit_events (
    id TEXT PRIMARY KEY,
    occurred_at DATETIME NOT NULL,
    actor_type VARCHAR(20) NOT NULL,
    actor_id TEXT,
    action VARCHAR(60) NOT NULL,
    reason TEXT,
    transaction_id TEXT,
    escrow_id TEXT,
    attempt_id TEXT,
    provider_reference VARCHAR(120),
    rail_name VARCHAR(40),
    correlation_id VARCHAR(320) NOT NULL,
    amount NUMERIC(12, 2),
    currency VARCHAR(3),
    details JSON NOT NULL DEFAULT '{}'
)
"""


def _db_with_audit_table() -> Session:
    db = _build_db_session()
    db.execute(text(_AUDIT_TABLE_DDL))
    db.commit()
    return db


def _events(db: Session) -> list[MoneyAuditEvent]:
    return list(
        db.execute(select(MoneyAuditEvent).order_by(MoneyAuditEvent.occurred_at.asc()))
        .scalars()
        .all()
    )


def test_payout_queue_and_execution_are_audited_with_correlation_ids() -> None:
    db = _db_with_audit_table()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        EscrowService(db).queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
        db.commit()
        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        run_payout_executor_once(db)

        events = _events(db)
        actions = [event.action for event in events]
        assert actions == ["payout_release_queued", "payout_release_succeeded"]

        queued, settled = events
        assert queued.actor_type == ACTOR_SYSTEM
        assert queued.transaction_id == transaction.id
        assert queued.amount == Decimal("2000.00")
        assert queued.reason == f"tx-release:{transaction.id}"

        assert settled.provider_reference
        assert settled.rail_name == "simulated"
        assert settled.correlation_id == build_correlation_id(
            transaction_id=transaction.id,
            provider_reference=settled.provider_reference,
            rail_name="simulated",
        )
    finally:
        db.close()


def test_failed_payout_is_audited_with_failure_reason() -> None:
    db = _db_with_audit_table()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        EscrowService(db).queue_release_full(
            transaction, purpose=f"sim:failed tx-release:{transaction.id}"
        )
        db.commit()

        run_payout_executor_once(db)

        failed = _events(db)[-1]
        assert failed.action == "payout_release_failed_definite"
        assert failed.details["failure_code"]
    finally:
        db.close()


def test_admin_initiated_payout_records_admin_actor_and_reason() -> None:
    db = _db_with_audit_table()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.ESCALATED_ADMIN_REVIEW)
        admin_id = uuid.uuid4()
        service = EscrowService(
            db,
            actor_type=ACTOR_ADMIN,
            actor_id=admin_id,
            audit_reason="seller shipped wrong item",
        )
        service.queue_refund_full(transaction, purpose=f"admin-refund:{transaction.id}")
        db.commit()

        event = _events(db)[0]
        assert event.action == "payout_refund_queued"
        assert event.actor_type == ACTOR_ADMIN
        assert event.actor_id == admin_id
        assert event.reason == "seller shipped wrong item"
    finally:
        db.close()


def test_audit_log_line_carries_correlation_fields(caplog: pytest.LogCaptureFixture) -> None:
    db = _build_db_session()
    try:
        transaction_id = uuid.uuid4()
        with caplog.at_level("INFO", logger="app.audit.money"):
            result = record_money_audit_event(
                db,
                action="funding_confirmed",
                actor_type="provider",
                transaction_id=transaction_id,
                provider_reference="loop-ref-77",
                rail_name="loop",
            )

        # Table is absent in this schema; the log line must still be emitted.
        assert result is None
        assert f"transaction_id={transaction_id}" in caplog.text
        assert "provider_reference=loop-ref-77" in caplog.text
        assert "rail=loop" in caplog.text
    finally:
        db.close()


def _audit_endpoint_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text(_AUDIT_TABLE_DDL))
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _authenticated_client(monkeypatch: pytest.MonkeyPatch, role: UserRole) -> TestClient:
    user = _build_user(role=role, phone="+254700000555")
    settings = get_settings()
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    return client


@pytest.fixture
def audit_db() -> Iterator[Session]:
    db = _audit_endpoint_db()

    def _override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_admin_audit_endpoint_requires_authentication(audit_db: Session) -> None:
    response = TestClient(app).get("/admin/audit/money-events")
    assert response.status_code == 401


def test_admin_audit_endpoint_rejects_non_admin(
    audit_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    assert client.get("/admin/audit/money-events").status_code == 403


def test_admin_audit_endpoint_filters_events(
    audit_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_txn = uuid.uuid4()
    base = datetime.now(UTC)
    for offset, (txn, action) in enumerate(
        [
            (target_txn, "payout_release_queued"),
            (target_txn, "payout_release_succeeded"),
            (uuid.uuid4(), "payout_refund_queued"),
        ]
    ):
        record_money_audit_event(
            audit_db,
            action=action,
            actor_type=ACTOR_SYSTEM,
            transaction_id=txn,
            rail_name="simulated",
            amount=Decimal("100.00"),
            currency="KES",
            occurred_at=base + timedelta(seconds=offset),
        )
    audit_db.commit()

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.get(
        "/admin/audit/money-events",
        params={"transaction_id": str(target_txn)},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["action"] for item in items] == [
        "payout_release_succeeded",
        "payout_release_queued",
    ]
    assert all(item["transaction_id"] == str(target_txn) for item in items)
    assert items[0]["correlation_id"].startswith(f"txn={target_txn}|")
