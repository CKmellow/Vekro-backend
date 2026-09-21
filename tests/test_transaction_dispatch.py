import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import transactions as transactions_router
from app.services.auth import ActiveSessionContext
from app.services.transaction import (
    TransactionDispatchForbiddenError,
    TransactionDispatchInvalidStateError,
    TransactionNotFoundForDispatchError,
)
from app.services.transaction import dispatch_transaction as dispatch_transaction_service
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self, transaction: Transaction | None = None) -> None:
        self.transaction = transaction
        self.added: list[Any] = []
        self.committed = False
        self.refreshed = False

    def get(self, _model: Any, transaction_id):
        if self.transaction is not None and transaction_id == self.transaction.id:
            return self.transaction
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        self.refreshed = self.transaction is obj


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


def _build_transaction(
    seller_id: uuid.UUID,
    *,
    status: TransactionStatus = TransactionStatus.LOCKED,
) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=seller_id,
        amount=Decimal("2400.00"),
        status=status,
        created_at=now,
        updated_at=now,
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


def test_dispatch_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(f"/transactions/{uuid.uuid4()}/dispatch")

    assert response.status_code == 401


def test_dispatch_rejects_buyer_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/dispatch",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_dispatch_allows_transaction_seller(monkeypatch) -> None:
    seller = _build_user(UserRole.SELLER)
    transaction = _build_transaction(
        seller.id,
        status=TransactionStatus.OUT_FOR_DELIVERY,
    )

    def fake_dispatch_transaction(_db, transaction_id, seller):
        assert transaction_id == transaction.id
        assert seller.role == UserRole.SELLER
        return transaction

    monkeypatch.setattr(transactions_router, "dispatch_transaction", fake_dispatch_transaction)

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{transaction.id}/dispatch",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(transaction.id)
    assert body["status"] == "out_for_delivery"


def test_dispatch_returns_404_when_transaction_missing(monkeypatch) -> None:
    def fake_dispatch_transaction(_db, transaction_id, seller):
        raise TransactionNotFoundForDispatchError("Transaction not found.")

    monkeypatch.setattr(transactions_router, "dispatch_transaction", fake_dispatch_transaction)

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/dispatch",
        headers=_csrf_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found."


def test_dispatch_returns_403_when_seller_not_owner(monkeypatch) -> None:
    def fake_dispatch_transaction(_db, transaction_id, seller):
        raise TransactionDispatchForbiddenError(
            "Only the transaction seller can dispatch this transaction."
        )

    monkeypatch.setattr(transactions_router, "dispatch_transaction", fake_dispatch_transaction)

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/dispatch",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only the transaction seller can dispatch this transaction."


def test_dispatch_returns_422_for_invalid_prior_state(monkeypatch) -> None:
    def fake_dispatch_transaction(_db, transaction_id, seller):
        raise TransactionDispatchInvalidStateError(
            "Transaction must be in locked state before dispatch."
        )

    monkeypatch.setattr(transactions_router, "dispatch_transaction", fake_dispatch_transaction)

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/dispatch",
        headers=_csrf_headers(),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Transaction must be in locked state before dispatch."


def test_service_dispatch_moves_locked_to_out_for_delivery() -> None:
    seller = _build_user(UserRole.SELLER)
    transaction = _build_transaction(seller.id, status=TransactionStatus.LOCKED)
    db = _FakeDb(transaction=transaction)

    dispatched = dispatch_transaction_service(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )

    assert dispatched is transaction
    assert dispatched.status == TransactionStatus.OUT_FOR_DELIVERY
    assert dispatched.dispatched_at is not None
    assert db.committed is True
    assert db.refreshed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert {item.user_id for item in notifications} == {transaction.buyer_id, transaction.seller_id}
    assert all(
        item.event_type == NotificationEventType.TRANSACTION_DISPATCHED for item in notifications
    )


def test_service_dispatch_rejects_non_owner_seller() -> None:
    owner = _build_user(UserRole.SELLER)
    other_seller = _build_user(UserRole.SELLER)
    transaction = _build_transaction(owner.id, status=TransactionStatus.LOCKED)
    db = _FakeDb(transaction=transaction)

    with pytest.raises(
        TransactionDispatchForbiddenError,
        match="Only the transaction seller can dispatch this transaction.",
    ):
        dispatch_transaction_service(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=other_seller,
        )

    assert db.committed is False


def test_service_dispatch_rejects_invalid_prior_state() -> None:
    seller = _build_user(UserRole.SELLER)
    transaction = _build_transaction(seller.id, status=TransactionStatus.AWAITING_PAYMENT)
    db = _FakeDb(transaction=transaction)

    with pytest.raises(
        TransactionDispatchInvalidStateError,
        match="Transaction must be in locked state before dispatch.",
    ):
        dispatch_transaction_service(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=seller,
        )

    assert db.committed is False


def test_service_dispatch_rejects_missing_transaction() -> None:
    seller = _build_user(UserRole.SELLER)
    db = _FakeDb(transaction=None)

    with pytest.raises(TransactionNotFoundForDispatchError, match="Transaction not found."):
        dispatch_transaction_service(
            cast(Session, db),
            transaction_id=uuid.uuid4(),
            seller=seller,
        )

    assert db.committed is False
