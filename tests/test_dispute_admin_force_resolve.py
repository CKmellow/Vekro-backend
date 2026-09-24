import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.dispute import AdminDecision, Dispute, DisputeStatus, DisputeType
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import disputes as disputes_router
from app.services.auth import ActiveSessionContext
from app.services.dispute import (
    AdminForceResolveResult,
    DisputeCaseInvalidStateError,
    DisputeCaseNotFoundError,
    force_resolve_dispute_case,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self, *, dispute: Dispute | None, transaction: Transaction | None):
        self.dispute = dispute
        self.transaction = transaction
        self.notifications: list[Notification] = []
        self.committed = False

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

    def add(self, obj):
        if isinstance(obj, Notification):
            self.notifications.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        _ = obj


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
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    return client


def _csrf_headers() -> dict[str, str]:
    settings = get_settings()
    return {settings.csrf_header_name: "csrf-token"}


def _build_transaction(status: TransactionStatus) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount=Decimal("4000.00"),
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
        reason="not_as_described",
        description="Received wrong model.",
        evidence={},
        opened_at=now - timedelta(days=2),
        escalated_at=now - timedelta(days=1),
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=1),
    )


def test_force_resolve_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/admin/disputes/{uuid.uuid4()}/force-resolve",
        json={"decision": "refund", "reason": "confirmed mismatch"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 401


def test_force_resolve_rejects_non_admin(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/admin/disputes/{uuid.uuid4()}/force-resolve",
        json={"decision": "refund", "reason": "confirmed mismatch"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_force_resolve_rejects_blank_reason(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.post(
        f"/admin/disputes/{uuid.uuid4()}/force-resolve",
        json={"decision": "refund", "reason": "   "},
        headers=_csrf_headers(),
    )

    assert response.status_code == 422


def test_force_resolve_returns_404_when_dispute_missing(monkeypatch) -> None:
    def fake_force_resolve_dispute_case(_db, dispute_id, decision, reason):
        _ = dispute_id
        _ = decision
        _ = reason
        raise DisputeCaseNotFoundError("missing")

    monkeypatch.setattr(
        disputes_router,
        "force_resolve_dispute_case",
        fake_force_resolve_dispute_case,
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.post(
        f"/admin/disputes/{uuid.uuid4()}/force-resolve",
        json={"decision": "refund", "reason": "confirmed mismatch"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Dispute case not found."


def test_force_resolve_returns_422_when_dispute_not_escalated(monkeypatch) -> None:
    def fake_force_resolve_dispute_case(_db, dispute_id, decision, reason):
        _ = dispute_id
        _ = decision
        _ = reason
        raise DisputeCaseInvalidStateError("invalid dispute state")

    monkeypatch.setattr(
        disputes_router,
        "force_resolve_dispute_case",
        fake_force_resolve_dispute_case,
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.post(
        f"/admin/disputes/{uuid.uuid4()}/force-resolve",
        json={"decision": "refund", "reason": "confirmed mismatch"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid dispute state"


def test_force_resolve_returns_decision_payload_for_admin(monkeypatch) -> None:
    now = datetime.now(UTC)
    dispute_id = uuid.uuid4()
    result = AdminForceResolveResult(
        dispute_id=dispute_id,
        transaction_id=uuid.uuid4(),
        decision=AdminDecision.REFUND,
        reason="confirmed mismatch",
        dispute_status=DisputeStatus.RESOLVED_REFUND,
        transaction_status=TransactionStatus.RESOLVED_REFUND,
        resolved_at=now,
        released_at=None,
        refunded_at=now,
    )

    monkeypatch.setattr(
        disputes_router,
        "force_resolve_dispute_case",
        lambda _db, dispute_id, decision, reason: result,
    )

    client = _authenticated_client(monkeypatch, UserRole.ADMIN)
    response = client.post(
        f"/admin/disputes/{dispute_id}/force-resolve",
        json={"decision": "refund", "reason": "confirmed mismatch"},
        headers=_csrf_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dispute_id"] == str(dispute_id)
    assert body["decision"] == AdminDecision.REFUND.value
    assert body["dispute_status"] == DisputeStatus.RESOLVED_REFUND.value
    assert body["transaction_status"] == TransactionStatus.RESOLVED_REFUND.value


def test_service_force_resolve_rejects_non_escalated_dispute() -> None:
    transaction = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    dispute = _build_dispute(transaction.id, DisputeStatus.OPEN)

    db = _FakeDb(dispute=dispute, transaction=transaction)

    try:
        force_resolve_dispute_case(
            cast(Session, db),
            dispute_id=dispute.id,
            decision=AdminDecision.REFUND,
            reason="validated by support",
        )
    except DisputeCaseInvalidStateError as exc:
        assert "escalated_admin_review" in str(exc)
    else:
        raise AssertionError("Expected DisputeCaseInvalidStateError")


def test_service_force_resolve_refund_updates_state_and_notifications() -> None:
    transaction = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    dispute = _build_dispute(transaction.id, DisputeStatus.ESCALATED_ADMIN_REVIEW)
    db = _FakeDb(dispute=dispute, transaction=transaction)

    result = force_resolve_dispute_case(
        cast(Session, db),
        dispute_id=dispute.id,
        decision=AdminDecision.REFUND,
        reason="validated by support",
    )

    assert db.committed is True
    assert transaction.status == TransactionStatus.RESOLVED_REFUND
    assert dispute.status == DisputeStatus.RESOLVED_REFUND
    assert dispute.admin_decision == AdminDecision.REFUND
    assert dispute.admin_reason == "validated by support"
    assert result.transaction_status == TransactionStatus.RESOLVED_REFUND
    assert result.dispute_status == DisputeStatus.RESOLVED_REFUND
    assert len(db.notifications) == 2
    assert all(
        notification.event_type == NotificationEventType.ADMIN_DECISION
        for notification in db.notifications
    )


def test_service_force_resolve_release_updates_state() -> None:
    transaction = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    dispute = _build_dispute(transaction.id, DisputeStatus.ESCALATED_ADMIN_REVIEW)
    db = _FakeDb(dispute=dispute, transaction=transaction)

    result = force_resolve_dispute_case(
        cast(Session, db),
        dispute_id=dispute.id,
        decision=AdminDecision.RELEASE,
        reason="seller evidence accepted",
    )

    assert transaction.status == TransactionStatus.RESOLVED_RELEASE
    assert dispute.status == DisputeStatus.RESOLVED_RELEASE
    assert transaction.released_at is not None
    assert transaction.refunded_at is None
    assert result.decision == AdminDecision.RELEASE


def test_service_force_resolve_split_updates_state_and_ratio() -> None:
    transaction = _build_transaction(TransactionStatus.ESCALATED_ADMIN_REVIEW)
    dispute = _build_dispute(transaction.id, DisputeStatus.ESCALATED_ADMIN_REVIEW)
    db = _FakeDb(dispute=dispute, transaction=transaction)

    result = force_resolve_dispute_case(
        cast(Session, db),
        dispute_id=dispute.id,
        decision=AdminDecision.SPLIT,
        reason="both parties partially at fault",
    )

    assert transaction.status == TransactionStatus.RESOLVED_SPLIT
    assert dispute.status == DisputeStatus.RESOLVED_SPLIT
    assert transaction.released_at is not None
    assert transaction.refunded_at is not None
    assert dispute.split_ratio == Decimal("0.5000")
    assert result.decision == AdminDecision.SPLIT
