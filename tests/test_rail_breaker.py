from datetime import UTC, datetime, timedelta

from app.core.settings import Settings
from app.models.rail_health import RailBreakerState, RailHealth
from app.services.custody.rail_breaker import (
    RailBreakerPolicy,
    blocked_rails_for_routing,
    record_rail_failure,
    record_rail_success,
    select_payout_rail_for_routing,
)
from app.services.custody.registry import build_custody_registry
from sqlalchemy import create_engine, select, text
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


def test_breaker_opens_after_threshold_and_persists_cooldown() -> None:
    db = _build_db_session()
    try:
        policy = RailBreakerPolicy(failure_threshold=2, cooldown_seconds=90)
        now = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)

        record_rail_failure(
            db,
            rail_name="loop",
            provider_name="loop",
            error_code="timeout",
            error_message="first timeout",
            now=now,
            policy=policy,
        )
        first = db.execute(select(RailHealth).where(RailHealth.rail_name == "loop")).scalar_one()
        assert first.breaker_state == RailBreakerState.CLOSED
        assert first.consecutive_failures == 1

        record_rail_failure(
            db,
            rail_name="loop",
            provider_name="loop",
            error_code="timeout",
            error_message="second timeout",
            now=now + timedelta(seconds=1),
            policy=policy,
        )
        second = db.execute(select(RailHealth).where(RailHealth.rail_name == "loop")).scalar_one()
        assert second.breaker_state == RailBreakerState.OPEN
        assert second.consecutive_failures == 2
        assert second.cooldown_until == now + timedelta(seconds=91)
    finally:
        db.close()


def test_open_breaker_transitions_half_open_after_cooldown_and_success_closes() -> None:
    db = _build_db_session()
    try:
        policy = RailBreakerPolicy(failure_threshold=1, cooldown_seconds=30)
        now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

        record_rail_failure(
            db,
            rail_name="loop",
            provider_name="loop",
            error_code="auth_error",
            error_message="invalid credentials",
            now=now,
            policy=policy,
        )

        blocked_before = blocked_rails_for_routing(
            db,
            rail_names=("loop",),
            now=now + timedelta(seconds=10),
        )
        assert blocked_before == {"loop"}

        blocked_after = blocked_rails_for_routing(
            db,
            rail_names=("loop",),
            now=now + timedelta(seconds=31),
        )
        assert blocked_after == set()

        half_open_row = db.execute(
            select(RailHealth).where(RailHealth.rail_name == "loop")
        ).scalar_one()
        assert half_open_row.breaker_state == RailBreakerState.HALF_OPEN

        record_rail_success(
            db,
            rail_name="loop",
            provider_name="loop",
            now=now + timedelta(seconds=32),
        )
        closed_row = db.execute(select(RailHealth).where(RailHealth.rail_name == "loop")).scalar_one()
        assert closed_row.breaker_state == RailBreakerState.CLOSED
        assert closed_row.consecutive_failures == 0
        assert closed_row.cooldown_until is None
    finally:
        db.close()


def test_open_breaker_is_excluded_from_payout_routing() -> None:
    db = _build_db_session()
    try:
        settings = _settings()
        registry = build_custody_registry(settings)
        now = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

        record_rail_failure(
            db,
            rail_name="loop",
            provider_name="loop",
            error_code="auth_error",
            error_message="invalid credentials",
            now=now,
            policy=RailBreakerPolicy(failure_threshold=1, cooldown_seconds=120),
        )

        selected_name, _ = select_payout_rail_for_routing(
            db,
            registry,
            now=now + timedelta(seconds=1),
        )
        assert selected_name == "simulated"
    finally:
        db.close()
