import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.listing import Listing
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import listings as listings_router
from app.schemas.listing import CreateListingRequest
from app.services.auth import ActiveSessionContext
from app.services.listing import ListingValidationError
from app.services.listing import create_listing as create_listing_service
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self) -> None:
        self.added = None
        self.committed = False
        self.refreshed = False

    def add(self, obj) -> None:
        self.added = obj

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        self.refreshed = obj is self.added


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


def test_create_listing_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        "/listings",
        json={
            "title": "Sack of Rice",
            "price": "3200.00",
            "is_serialized": False,
            "dispute_policy": {},
        },
    )

    assert response.status_code == 401


def test_create_listing_rejects_buyer_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)

    response = client.post(
        "/listings",
        headers=_csrf_headers(),
        json={
            "title": "Sack of Rice",
            "price": "3200.00",
            "is_serialized": False,
            "dispute_policy": {},
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "You are not authorized to access this resource."


def test_create_listing_allows_seller_user(monkeypatch) -> None:
    def fake_create_listing(_db, seller, payload):
        return Listing(
            id=uuid.uuid4(),
            seller_id=seller.id,
            title=payload.title,
            price=payload.price,
            is_serialized=payload.is_serialized,
            unique_id=payload.unique_id,
            dispute_policy=payload.dispute_policy,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    monkeypatch.setattr(listings_router, "create_listing", fake_create_listing)
    client = _authenticated_client(monkeypatch, UserRole.SELLER)

    response = client.post(
        "/listings",
        headers=_csrf_headers(),
        json={
            "title": "Serialized Android Phone",
            "price": "12000.00",
            "is_serialized": True,
            "unique_id": "IMEI-998877",
            "dispute_policy": {"resolution": "seller_review"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Serialized Android Phone"
    assert body["is_serialized"] is True
    assert body["unique_id"] == "IMEI-998877"
    assert body["dispute_policy"]["resolution"] == "seller_review"


def test_create_listing_validation_errors_are_deterministic(monkeypatch) -> None:
    def fake_create_listing(_db, seller, payload):
        raise ListingValidationError("unique_id is required for serialized listings.")

    monkeypatch.setattr(listings_router, "create_listing", fake_create_listing)
    client = _authenticated_client(monkeypatch, UserRole.SELLER)

    response = client.post(
        "/listings",
        headers=_csrf_headers(),
        json={
            "title": "Serialized Android Phone",
            "price": "12000.00",
            "is_serialized": True,
            "dispute_policy": {"resolution": "seller_review"},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unique_id is required for serialized listings."


def test_service_requires_unique_id_for_serialized_listing() -> None:
    db = _FakeDb()
    payload = CreateListingRequest(
        title="Serialized Android Phone",
        price=Decimal("12000.00"),
        is_serialized=True,
        dispute_policy={"resolution": "seller_review"},
    )

    with pytest.raises(
        ListingValidationError,
        match="unique_id is required for serialized listings.",
    ):
        create_listing_service(
            cast(Session, db),
            seller=_build_user(UserRole.SELLER),
            payload=payload,
        )

    assert db.added is None
    assert db.committed is False


def test_service_requires_resolution_policy_for_serialized_listing() -> None:
    db = _FakeDb()
    payload = CreateListingRequest(
        title="Serialized Android Phone",
        price=Decimal("12000.00"),
        is_serialized=True,
        unique_id="IMEI-998877",
        dispute_policy={},
    )

    with pytest.raises(
        ListingValidationError,
        match="dispute_policy.resolution is required for serialized listings.",
    ):
        create_listing_service(
            cast(Session, db),
            seller=_build_user(UserRole.SELLER),
            payload=payload,
        )

    assert db.added is None
    assert db.committed is False


def test_service_creates_listing_for_seller_user() -> None:
    db = _FakeDb()
    payload = CreateListingRequest(
        title="Sack of Rice",
        price=Decimal("3200.00"),
        is_serialized=False,
        dispute_policy={},
    )

    listing = create_listing_service(
        cast(Session, db),
        seller=_build_user(UserRole.SELLER),
        payload=payload,
    )

    assert db.added is listing
    assert db.committed is True
    assert db.refreshed is True
    assert listing.title == "Sack of Rice"
    assert listing.is_serialized is False
