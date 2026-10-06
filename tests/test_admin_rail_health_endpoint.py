import uuid
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import Settings, get_settings
from app.db.session import get_db
from app.main import app
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.custody.rail_breaker import RailBreakerPolicy, record_rail_failure
from app.services.custody.registry import build_custody_registry
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def _build_db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("""
                CREATE TABLE rail_health (
                    id TEXT PRIMARY KEY,
                    rail_name VARCHAR(40) NOT NULL,
                    provider_name VARCHAR(40),
                    breaker_state VARCHAR(20) NOT NULL DEFAULT 'closed',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error_code VARCHAR(64),
                    last_error_message VARCHAR(255),
                    opened_at DATETIME,
                    last_success_at DATETIME,
                    last_failure_at DATETIME,
                    cooldown_until DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """))
        connection.execute(text("""
                CREATE UNIQUE INDEX ix_rail_health_rail_name
                ON rail_health (rail_name)
                """))

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return session_factory()


def _settings(**overrides) -> Settings:
    base = {
        "secret_key": "test-secret-key",
        "database_url": "postgresql+psycopg://user:pass@localhost/testdb",
        "alembic_database_url": "postgresql+psycopg://user:pass@localhost/testdb",
        "environment": "development",
        "custody_mode": "tier_2",
        "custody_collection_rail_priority": "simulated,loop",
        "custody_payout_rail_priority": "loop,simulated",
        "allow_live_payouts": False,
        "loop_enabled": True,
        "loop_base_url": "https://sandbox.loop.example",
        "loop_client_id": "loop-client",
        "loop_client_secret": "loop-secret",
        "loop_shortcode": "600111",
        "loop_passkey": "loop-passkey",
        "pesapal_enabled": False,
        "pesapal_base_url": "",
        "pesapal_consumer_key": "",
        "pesapal_consumer_secret": "",
        "pesapal_callback_url": "",
        "pesapal_ipn_id": "",
        "intasend_enabled": False,
        "intasend_base_url": "",
        "intasend_publishable_key": "",
        "intasend_secret_key": "",
        "intasend_webhook_secret": "",
        "econfirm_enabled": False,
        "econfirm_base_url": "",
        "econfirm_api_key": "",
        "econfirm_api_secret": "",
    }
    base.update(overrides)
    return Settings.model_validate(base)


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


def _authenticated_client(monkeypatch: pytest.MonkeyPatch, role: UserRole) -> TestClient:
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


def test_admin_rail_health_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/admin/rails/health")
    assert response.status_code == 401


def test_admin_rail_health_rejects_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_db_session()
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = build_custody_registry(_settings())
        app.dependency_overrides[get_db] = _override_get_db

        client = _authenticated_client(monkeypatch, UserRole.BUYER)
        response = client.get("/admin/rails/health")
        assert response.status_code == 403
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_admin_rail_health_reports_mode_capabilities_and_breaker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_db_session()
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        registry = build_custody_registry(_settings())
        app.state.custody_registry = registry
        app.dependency_overrides[get_db] = _override_get_db

        record_rail_failure(
            db,
            rail_name="loop",
            provider_name="loop",
            error_code="auth_error",
            error_message="invalid credentials",
            now=datetime(2026, 10, 5, 16, 0, tzinfo=UTC),
            policy=RailBreakerPolicy(failure_threshold=1, cooldown_seconds=86400),
        )

        client = _authenticated_client(monkeypatch, UserRole.ADMIN)
        response = client.get("/admin/rails/health")

        assert response.status_code == 200
        body = response.json()
        assert body["custody_mode"] == "tier_2"
        assert body["holds_funds_structurally"] is False
        assert body["collection_priority"] == ["simulated", "loop"]
        assert body["payout_priority"] == ["loop", "simulated"]

        loop_entry = next(item for item in body["rails"] if item["rail_name"] == "loop")
        assert loop_entry["breaker_state"] == "open"
        assert loop_entry["routable"] is False
        assert loop_entry["consecutive_failures"] >= 1
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()
