import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import main as app_main
from app.core.security import generate_session_token, hash_password, hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.main import app
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.schemas.auth import LoginRequest
from app.services.auth import (
    AccountLockedError,
    ActiveSessionContext,
    InvalidCredentialsError,
    authenticate_and_create_session,
    is_valid_csrf_for_session,
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct-horse-battery"


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, UserSession.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _user(db: Session) -> User:
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        name="Bea Buyer",
        phone=f"+2547{uuid.uuid4().int % 10**8:08d}",
        role=UserRole.BUYER,
        password_hash=hash_password(PASSWORD),
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    return user


def _login(db: Session, user: User, password: str, client: str) -> None:
    authenticate_and_create_session(
        db,
        LoginRequest(phone=user.phone, password=password),
        client_identifier=client,
    )


def test_account_locks_after_configured_failures_and_rejects_correct_password() -> None:
    db = _db()
    user = _user(db)
    max_attempts = get_settings().login_lockout_max_attempts
    # Fresh client per attempt keeps the per-client rate limiter out of the way.
    clients = iter(f"client-{i}" for i in range(max_attempts + 2))

    for _ in range(max_attempts - 1):
        with pytest.raises(InvalidCredentialsError):
            _login(db, user, "wrong-password", next(clients))
    assert user.locked_until is None

    with pytest.raises(AccountLockedError):
        _login(db, user, "wrong-password", next(clients))
    assert user.locked_until is not None

    with pytest.raises(AccountLockedError):
        _login(db, user, PASSWORD, next(clients))


def test_successful_login_resets_failed_attempt_counter() -> None:
    db = _db()
    user = _user(db)

    with pytest.raises(InvalidCredentialsError):
        _login(db, user, "wrong-password", "client-a")
    assert user.failed_login_attempts == 1

    _login(db, user, PASSWORD, "client-b")
    assert user.failed_login_attempts == 0


def test_issued_csrf_token_is_only_valid_for_its_own_session() -> None:
    db = _db()
    user = _user(db)

    first = authenticate_and_create_session(
        db, LoginRequest(phone=user.phone, password=PASSWORD), client_identifier="c1"
    )
    second = authenticate_and_create_session(
        db, LoginRequest(phone=user.phone, password=PASSWORD), client_identifier="c2"
    )
    sessions = db.query(UserSession).order_by(UserSession.created_at).all()
    first_session = next(s for s in sessions if s.csrf_token_hash == _hash(first.csrf_token))

    assert is_valid_csrf_for_session(first.csrf_token, first_session) is True
    assert is_valid_csrf_for_session(second.csrf_token, first_session) is False
    assert is_valid_csrf_for_session(generate_session_token(), first_session) is False


def _hash(token: str) -> str:
    return hash_session_token(token, get_settings().secret_key)


def _client_with_session(monkeypatch: pytest.MonkeyPatch, csrf_cookie: str) -> TestClient:
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        name="Sam Seller",
        phone="+254700000201",
        role=UserRole.SELLER,
        password_hash="unused",
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=now,
        updated_at=now,
    )
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=_hash("session-token"),
        csrf_token_hash=_hash("issued-csrf-token"),
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )
    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "session-token")
    client.cookies.set(settings.csrf_cookie_name, csrf_cookie)
    return client


def test_middleware_rejects_csrf_header_that_differs_from_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_session(monkeypatch, csrf_cookie="issued-csrf-token")

    response = client.post(
        "/listings",
        json={"title": "Phone", "price": "100.00"},
        headers={get_settings().csrf_header_name: "attacker-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token mismatch."


def test_middleware_rejects_matching_pair_not_issued_for_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_session(monkeypatch, csrf_cookie="forged-token")

    response = client.post(
        "/listings",
        json={"title": "Phone", "price": "100.00"},
        headers={get_settings().csrf_header_name: "forged-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token invalid."
