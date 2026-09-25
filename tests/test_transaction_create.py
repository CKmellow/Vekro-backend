import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import transactions as transactions_router
from app.schemas.transaction import CreateTransactionRequest
from app.services.auth import ActiveSessionContext
from app.services.transaction import (
    ListingNotFoundForTransactionError,
    PaymentInitiationError,
)
from app.services.transaction import (
    create_transaction as create_transaction_service,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self, listing: Listing | None = None) -> None:
        self.listing = listing
        self.added: list[Any] = []
        self.committed = False
        self.refreshed_obj = None

    def get(self, _model: Any, listing_id):
        if self.listing is not None and listing_id == self.listing.id:
            return self.listing
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        self.refreshed_obj = obj


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


def _build_listing(seller_id: uuid.UUID | None = None) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=seller_id or uuid.uuid4(),
        title="Serialized Router",
        price=Decimal("15000.00"),
        is_serialized=True,
        unique_id="RT-112233",
        dispute_policy={"resolution": "seller_review"},
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


def test_create_transaction_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        "/transactions",
        json={
            "listing_id": str(uuid.uuid4()),
            "amount": "1000.00",
        },
    )

    assert response.status_code == 401


def test_create_transaction_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        "/transactions",
        headers=_csrf_headers(),
        json={
            "listing_id": str(uuid.uuid4()),
            "amount": "1000.00",
        },
    )

    assert response.status_code == 403


def test_create_transaction_allows_buyer_and_sets_status(monkeypatch) -> None:
    listing = _build_listing()

    def fake_create_transaction(_db, buyer, payload):
        return Transaction(
            id=uuid.uuid4(),
            listing_id=listing.id,
            buyer_id=buyer.id,
            seller_id=listing.seller_id,
            amount=payload.amount,
            status=TransactionStatus.AWAITING_PAYMENT,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(transactions_router, "create_transaction", fake_create_transaction)

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        "/transactions",
        headers=_csrf_headers(),
        json={
            "listing_id": str(listing.id),
            "amount": "1000.00",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["listing_id"] == str(listing.id)
    assert body["amount"] == "1000.00"
    assert body["status"] == "awaiting_payment"


def test_create_transaction_returns_404_when_listing_missing(monkeypatch) -> None:
    def fake_create_transaction(_db, buyer, payload):
        raise ListingNotFoundForTransactionError("Listing not found.")

    monkeypatch.setattr(transactions_router, "create_transaction", fake_create_transaction)

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        "/transactions",
        headers=_csrf_headers(),
        json={
            "listing_id": str(uuid.uuid4()),
            "amount": "1000.00",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Listing not found."


def test_service_creates_transaction_with_participant_linkage() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing()
    db = _FakeDb(listing=listing)

    payload = CreateTransactionRequest(
        listing_id=listing.id,
        amount=Decimal("2200.00"),
    )

    created = create_transaction_service(
        cast(Session, db),
        buyer=buyer,
        payload=payload,
    )

    assert db.added[0] is created
    assert db.committed is True
    assert db.refreshed_obj is created
    assert created.status == TransactionStatus.AWAITING_PAYMENT
    assert created.amount == Decimal("2200.00")
    assert created.buyer_id == buyer.id
    assert created.seller_id == listing.seller_id

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(
        item.event_type == NotificationEventType.TRANSACTION_CREATED for item in notifications
    )
    assert {item.user_id for item in notifications} == {created.buyer_id, created.seller_id}


def test_service_rejects_missing_listing() -> None:
    buyer = _build_user(UserRole.BUYER)
    db = _FakeDb(listing=None)

    payload = CreateTransactionRequest(
        listing_id=uuid.uuid4(),
        amount=Decimal("2200.00"),
    )

    with pytest.raises(ListingNotFoundForTransactionError, match="Listing not found."):
        create_transaction_service(
            cast(Session, db),
            buyer=buyer,
            payload=payload,
        )


def test_service_invokes_payment_gateway_interface() -> None:
    class _FakeGateway:
        def __init__(self) -> None:
            self.called = False

        def initiate_stk_push(self, request):
            self.called = True
            return request

    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing()
    db = _FakeDb(listing=listing)
    payload = CreateTransactionRequest(
        listing_id=listing.id,
        amount=Decimal("2200.00"),
    )
    gateway = _FakeGateway()

    create_transaction_service(
        cast(Session, db),
        buyer=buyer,
        payload=payload,
        payment_gateway=gateway,
    )

    assert gateway.called is True


def test_service_raises_payment_initiation_error_when_gateway_fails() -> None:
    class _FailingGateway:
        def initiate_stk_push(self, request):
            raise RuntimeError("transport down")

    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing()
    db = _FakeDb(listing=listing)
    payload = CreateTransactionRequest(
        listing_id=listing.id,
        amount=Decimal("2200.00"),
    )

    with pytest.raises(PaymentInitiationError, match="Failed to initiate payment transport."):
        create_transaction_service(
            cast(Session, db),
            buyer=buyer,
            payload=payload,
            payment_gateway=_FailingGateway(),
        )
