import uuid
from datetime import UTC, datetime, timedelta

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from fastapi.testclient import TestClient


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


def test_buyer_route_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/protected/buyer")

    assert response.status_code == 401


def test_buyer_route_allows_buyer(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)

    response = client.get("/protected/buyer")

    assert response.status_code == 200
    assert response.json()["role"] == "buyer"


def test_buyer_route_rejects_seller(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)

    response = client.get("/protected/buyer")

    assert response.status_code == 403


def test_seller_route_allows_seller(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)

    response = client.get("/protected/seller")

    assert response.status_code == 200
    assert response.json()["role"] == "seller"


def test_admin_route_allows_admin(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.ADMIN)

    response = client.get("/protected/admin")

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_admin_route_rejects_buyer(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)

    response = client.get("/protected/admin")

    assert response.status_code == 403
