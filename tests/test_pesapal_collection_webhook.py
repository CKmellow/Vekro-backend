import json
from collections.abc import Iterator
from types import SimpleNamespace

from app.db.session import get_db
from app.main import app
from app.models.provider_event import ProviderEvent
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_webhook import process_pesapal_collection_webhook
from fastapi.testclient import TestClient
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
                CREATE TABLE provider_events (
                    id TEXT PRIMARY KEY,
                    provider_name VARCHAR(40) NOT NULL,
                    rail_name VARCHAR(40),
                    event_type VARCHAR(80) NOT NULL,
                    dedupe_key VARCHAR(120) NOT NULL,
                    external_event_id VARCHAR(120),
                    request_snapshot JSON NOT NULL DEFAULT '{}',
                    response_snapshot JSON NOT NULL DEFAULT '{}',
                    payload JSON NOT NULL DEFAULT '{}',
                    received_at DATETIME NOT NULL,
                    processed_at DATETIME,
                    created_at DATETIME NOT NULL
                )
                """))
        connection.execute(text("""
                CREATE UNIQUE INDEX ux_provider_events_provider_dedupe_key
                ON provider_events (provider_name, dedupe_key)
                """))
        connection.execute(text("""
                CREATE INDEX ix_provider_events_provider_name
                ON provider_events (provider_name)
                """))

    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return SessionLocal()


def test_webhook_uses_inquiry_confirmation_not_callback_claim() -> None:
    db = _build_db_session()
    try:
        payload = {
            "OrderTrackingId": "pesapal-track-501",
            "OrderNotificationType": "IPNCHANGE",
            "status": "COMPLETED",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        inquiry_calls: list[str] = []

        def _inquiry_status(provider_reference: str) -> CollectionResult:
            inquiry_calls.append(provider_reference)
            return CollectionResult(
                outcome=CollectionOutcome.FAILED_DEFINITE,
                provider_reference=provider_reference,
                raw_status="failed",
                message="Declined by provider",
            )

        result = process_pesapal_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            inquiry_status_fn=_inquiry_status,
        )

        assert result.accepted is True
        assert result.duplicate is False
        assert result.inquiry_outcome == CollectionOutcome.FAILED_DEFINITE
        assert inquiry_calls == ["pesapal-track-501"]

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.dedupe_key == "collection:pesapal-track-501"
        assert event.response_snapshot["funding_confirmed"] is False
        assert event.response_snapshot["inquiry"]["outcome"] == "FAILED_DEFINITE"
    finally:
        db.close()


def test_webhook_processing_is_idempotent() -> None:
    db = _build_db_session()
    try:
        payload = {
            "OrderTrackingId": "pesapal-track-502",
            "OrderNotificationType": "IPNCHANGE",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        call_count = 0

        def _inquiry_status(provider_reference: str) -> CollectionResult:
            nonlocal call_count
            call_count += 1
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED,
                provider_reference=provider_reference,
                raw_status="completed",
                message="Settled",
            )

        first = process_pesapal_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            inquiry_status_fn=_inquiry_status,
        )
        second = process_pesapal_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            inquiry_status_fn=_inquiry_status,
        )

        assert first.duplicate is False
        assert second.duplicate is True
        assert call_count == 1
        events = list(db.execute(select(ProviderEvent)).scalars().all())
        assert len(events) == 1
    finally:
        db.close()


def test_webhook_handles_missing_tracking_id_without_inquiry() -> None:
    db = _build_db_session()
    try:
        payload = {
            "OrderNotificationType": "IPNCHANGE",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        def _unexpected_inquiry(_provider_reference: str) -> CollectionResult:
            raise AssertionError("Inquiry should not run when tracking id is missing.")

        result = process_pesapal_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            inquiry_status_fn=_unexpected_inquiry,
        )

        assert result.accepted is True
        assert result.provider_reference is None
        assert result.inquiry_outcome is None

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.response_snapshot["state"] == "ignored_missing_provider_reference"
    finally:
        db.close()


def test_pesapal_webhook_endpoint_processes_callback_query_trigger() -> None:
    db = _build_db_session()
    fake_rail = SimpleNamespace(
        get_funding_status=lambda provider_reference: CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="completed",
            message="Settled",
        )
    )
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = SimpleNamespace(collection_rails={"pesapal": fake_rail})
        app.dependency_overrides[get_db] = _override_get_db

        client = TestClient(app)
        response = client.get(
            "/api/webhooks/pesapal/callback",
            params={
                "OrderTrackingId": "pesapal-track-503",
                "OrderNotificationType": "IPNCHANGE",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["duplicate"] is False
        assert body["provider_reference"] == "pesapal-track-503"
        assert body["inquiry_outcome"] == "SUCCEEDED"

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.dedupe_key == "collection:pesapal-track-503"
        assert event.response_snapshot["funding_confirmed"] is True
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_pesapal_webhook_endpoint_returns_503_when_rail_unavailable() -> None:
    db = _build_db_session()
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = SimpleNamespace(collection_rails={})
        app.dependency_overrides[get_db] = _override_get_db

        client = TestClient(app)
        response = client.get("/api/webhooks/pesapal/callback")

        assert response.status_code == 503
        assert response.json()["detail"] == "Pesapal collection rail is unavailable."
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()