import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import disputes as disputes_router
from app.services.auth import ActiveSessionContext
from app.services.dispute import EscalationQueueItem, list_escalated_disputes
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeQueueResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query):
        _ = query
        return _FakeQueueResult(self._rows)


def _build_user(role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} User",
        phone="+254700123456",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_session(user_id: uuid.UUID) -> UserSession:
    settings = get_settings()
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _authenticated_client(monkeypatch, role: UserRole) -> TestClient:
    user = _build_user(role)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "session-token")
    return client


def _build_transaction(status: TransactionStatus) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount=Decimal("2500.00"),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _build_dispute(transaction_id: uuid.UUID, status: DisputeStatus) -> Dispute:
    now = datetime.now(UTC)
    return Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction_id,
        opened_by_user_id=uuid.uuid4(),
        dispute_type=DisputeType.FUNCTIONAL,
        status=status,
        reason="not_working",
        description="Fails to boot.",
        evidence={},
        opened_at=now,
        escalated_at=now if status == DisputeStatus.ESCALATED_ADMIN_REVIEW else None,
        created_at=now,
        updated_at=now,
    )


def test_escalation_queue_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/admin/disputes/escalation-queue")

    assert response.status_code == 401


def test_escalation_queue_rejects_non_admin(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.get("/admin/disputes/escalation-queue")

    assert response.status_code == 403


def test_escalation_queue_returns_summary_for_admin(monkeypatch) -> None:
    item = EscalationQueueItem(
        dispute_id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        dispute_type=DisputeType.FUNCTIONAL,
        dispute_status=DisputeStatus.ESCALATED_ADMIN_REVIEW,
        reason="not_working",
        description="Fails to boot.",
        opened_at=datetime.now(UTC),
        escalated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    monkeypatch.setattr(
        disputes_router,
        "list_escalated_disputes",
        lambda _db: [item],
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.get("/admin/disputes/escalation-queue")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["dispute_status"] == DisputeStatus.ESCALATED_ADMIN_REVIEW.value
    assert body[0]["reason"] == "not_working"
    assert body[0]["description"] == "Fails to boot."
    assert "transaction_id" in body[0]
    assert "listing_id" in body[0]
    assert "buyer_id" in body[0]
    assert "seller_id" in body[0]


def test_service_escalation_queue_filters_only_escalated_status() -> None:
    escalated_txn = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    escalated_dispute = _build_dispute(escalated_txn.id, DisputeStatus.ESCALATED_ADMIN_REVIEW)

    open_txn = _build_transaction(TransactionStatus.DISPUTED_FUNCTIONAL)
    open_dispute = _build_dispute(open_txn.id, DisputeStatus.OPEN)

    db = _FakeDb(rows=[(escalated_dispute, escalated_txn), (open_dispute, open_txn)])

    queue = list_escalated_disputes(cast(Session, db))

    assert len(queue) == 1
    assert queue[0].dispute_id == escalated_dispute.id
    assert queue[0].transaction_id == escalated_txn.id
    assert queue[0].dispute_status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert queue[0].reason == "not_working"
