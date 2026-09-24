import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import disputes as disputes_router
from app.services.auth import ActiveSessionContext
from app.services.dispute import (
    DisputeCaseNotFoundError,
    DisputeCaseTimeline,
    DisputeTimelineEvent,
    get_dispute_case_timeline,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeNotificationResult:
    def __init__(self, notifications):
        self._notifications = notifications

    def scalars(self):
        return self

    def all(self):
        return self._notifications


class _FakeDb:
    def __init__(self, *, dispute: Dispute | None, transaction: Transaction | None, notifications):
        self.dispute = dispute
        self.transaction = transaction
        self.notifications = notifications

    def get(self, model, model_id):
        if model is Dispute:
            if self.dispute and self.dispute.id == model_id:
                return self.dispute
            return None
        if model is Transaction:
            if self.transaction and self.transaction.id == model_id:
                return self.transaction
            return None
        return None

    def execute(self, query):
        _ = query
        return _FakeNotificationResult(self.notifications)


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
        amount=Decimal("3200.00"),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _build_dispute(transaction_id: uuid.UUID) -> Dispute:
    now = datetime.now(UTC)
    return Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction_id,
        opened_by_user_id=uuid.uuid4(),
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.ESCALATED_ADMIN_REVIEW,
        reason="not_as_described",
        description="Device differs from listing.",
        evidence={},
        opened_at=now - timedelta(days=2),
        escalated_at=now - timedelta(days=1),
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
    )


def test_dispute_timeline_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get(f"/admin/disputes/{uuid.uuid4()}/timeline")

    assert response.status_code == 401


def test_dispute_timeline_rejects_non_admin(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.get(f"/admin/disputes/{uuid.uuid4()}/timeline")

    assert response.status_code == 403


def test_dispute_timeline_returns_404_when_not_found(monkeypatch) -> None:
    def fake_get_dispute_case_timeline(_db, dispute_id):
        raise DisputeCaseNotFoundError("missing")

    monkeypatch.setattr(
        disputes_router,
        "get_dispute_case_timeline",
        fake_get_dispute_case_timeline,
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.get(f"/admin/disputes/{uuid.uuid4()}/timeline")

    assert response.status_code == 404
    assert response.json()["detail"] == "Dispute case not found."


def test_dispute_timeline_returns_case_payload_for_admin(monkeypatch) -> None:
    dispute_id = uuid.uuid4()
    now = datetime.now(UTC)
    case = DisputeCaseTimeline(
        dispute_id=dispute_id,
        transaction_id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        opened_by_user_id=uuid.uuid4(),
        transaction_status=TransactionStatus.ESCALATED_ADMIN_REVIEW,
        amount=Decimal("3200.00"),
        dispute_type=DisputeType.FUNCTIONAL,
        dispute_status=DisputeStatus.ESCALATED_ADMIN_REVIEW,
        reason="not_as_described",
        description="Device differs from listing.",
        opened_at=now - timedelta(days=2),
        escalated_at=now - timedelta(days=1),
        resolved_at=None,
        timeline_events=[
            DisputeTimelineEvent(
                source="dispute",
                event_type="dispute_opened",
                at=now - timedelta(days=2),
                title="Dispute opened",
                message="Dispute case was opened.",
                payload={"dispute_status": "open"},
            )
        ],
    )

    monkeypatch.setattr(
        disputes_router,
        "get_dispute_case_timeline",
        lambda _db, dispute_id: case,
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.get(f"/admin/disputes/{dispute_id}/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["dispute_id"] == str(dispute_id)
    assert body["dispute_status"] == DisputeStatus.ESCALATED_ADMIN_REVIEW.value
    assert body["transaction_status"] == TransactionStatus.ESCALATED_ADMIN_REVIEW.value
    assert body["reason"] == "not_as_described"
    assert len(body["timeline_events"]) == 1
    assert body["timeline_events"][0]["event_type"] == "dispute_opened"


def test_service_dispute_case_timeline_includes_notification_events() -> None:
    transaction = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    dispute = _build_dispute(transaction.id)

    notification = Notification(
        id=uuid.uuid4(),
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.DISPUTE_OPENED,
        title="Dispute opened",
        message="Case opened by buyer.",
        payload={"transition": {"to": "disputed_functional"}},
        created_at=datetime.now(UTC) - timedelta(hours=3),
        updated_at=datetime.now(UTC) - timedelta(hours=3),
    )

    db = _FakeDb(
        dispute=dispute,
        transaction=transaction,
        notifications=[notification],
    )

    case = get_dispute_case_timeline(cast(Session, db), dispute_id=dispute.id)

    assert case.dispute_id == dispute.id
    assert case.transaction_id == transaction.id
    assert case.buyer_id == transaction.buyer_id
    assert case.seller_id == transaction.seller_id
    assert case.dispute_status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert len(case.timeline_events) >= 3
    assert any(event.event_type == "dispute_opened" for event in case.timeline_events)
    assert any(
        event.event_type == NotificationEventType.DISPUTE_OPENED.value
        for event in case.timeline_events
    )
