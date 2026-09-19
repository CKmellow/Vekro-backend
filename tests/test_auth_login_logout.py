import uuid
from datetime import UTC, datetime, timedelta

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import auth as auth_router
from app.services.auth import (
    AccountLockedError,
    ActiveSessionContext,
    AuthenticatedSession,
    InvalidCredentialsError,
    TooManyLoginAttemptsError,
)
from fastapi.testclient import TestClient


def _build_user(phone: str, role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name="Login Test User",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_session(user_id: uuid.UUID, *, csrf_token: str = "csrf-token") -> UserSession:
    settings = get_settings()
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token(csrf_token, settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_login_success_sets_session_and_csrf_cookies(monkeypatch) -> None:
    def fake_authenticate_and_create_session(_db, payload, **_kwargs):
        return AuthenticatedSession(
            user=_build_user(payload.phone, UserRole.BUYER),
            session_token="session-token",
            csrf_token="csrf-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    monkeypatch.setattr(
        auth_router,
        "authenticate_and_create_session",
        fake_authenticate_and_create_session,
    )

    client = TestClient(app)
    response = client.post(
        "/auth/login",
        json={
            "phone": "+254700123456",
            "password": "strong-pass-123",
        },
    )

    settings = get_settings()
    assert response.status_code == 200
    assert response.cookies.get(settings.session_cookie_name) == "session-token"
    assert response.cookies.get(settings.csrf_cookie_name) == "csrf-token"


def test_login_invalid_credentials_returns_401(monkeypatch) -> None:
    def fake_authenticate_and_create_session(_db, _payload, **_kwargs):
        raise InvalidCredentialsError

    monkeypatch.setattr(
        auth_router,
        "authenticate_and_create_session",
        fake_authenticate_and_create_session,
    )

    client = TestClient(app)
    response = client.post(
        "/auth/login",
        json={
            "phone": "+254700123456",
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid phone or password."


def test_login_rate_limited_returns_429(monkeypatch) -> None:
    def fake_authenticate_and_create_session(_db, _payload, **_kwargs):
        raise TooManyLoginAttemptsError(30)

    monkeypatch.setattr(
        auth_router,
        "authenticate_and_create_session",
        fake_authenticate_and_create_session,
    )

    client = TestClient(app)
    response = client.post(
        "/auth/login",
        json={
            "phone": "+254700123456",
            "password": "wrong-password",
        },
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "30"


def test_login_locked_account_returns_429(monkeypatch) -> None:
    def fake_authenticate_and_create_session(_db, _payload, **_kwargs):
        raise AccountLockedError(120)

    monkeypatch.setattr(
        auth_router,
        "authenticate_and_create_session",
        fake_authenticate_and_create_session,
    )

    client = TestClient(app)
    response = client.post(
        "/auth/login",
        json={
            "phone": "+254700123456",
            "password": "wrong-password",
        },
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "120"


def test_logout_revokes_cookie_session(monkeypatch) -> None:
    seen: dict[str, str | None] = {}
    user = _build_user("+254700123456", UserRole.BUYER)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    def fake_invalidate_session(_db, session_token):
        seen["session_token"] = session_token
        return True

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    monkeypatch.setattr(auth_router, "invalidate_session", fake_invalidate_session)

    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "existing-session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")

    response = client.post(
        "/auth/logout",
        headers={settings.csrf_header_name: "csrf-token"},
    )

    assert response.status_code == 204
    assert seen.get("session_token") == "existing-session-token"


def test_logout_without_cookie_still_returns_204(monkeypatch) -> None:
    seen: dict[str, str | None] = {}

    def fake_invalidate_session(_db, session_token):
        seen["session_token"] = session_token
        return False

    monkeypatch.setattr(auth_router, "invalidate_session", fake_invalidate_session)

    client = TestClient(app)
    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert seen.get("session_token") is None


def test_logout_missing_csrf_header_is_rejected(monkeypatch) -> None:
    seen: dict[str, str | None] = {}
    user = _build_user("+254700123456", UserRole.BUYER)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    def fake_invalidate_session(_db, session_token):
        seen["session_token"] = session_token
        return True

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    monkeypatch.setattr(auth_router, "invalidate_session", fake_invalidate_session)

    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "existing-session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")

    response = client.post("/auth/logout")

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing."
    assert seen == {}


def test_me_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/auth/me")

    assert response.status_code == 401


def test_me_with_valid_session_returns_profile(monkeypatch) -> None:
    user = _build_user("+254700123456", UserRole.BUYER)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)

    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "existing-session-token")

    response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["phone"] == "+254700123456"
