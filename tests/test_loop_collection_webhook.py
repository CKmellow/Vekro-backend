import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from app.db.session import get_db
from app.main import app
from app.models.provider_event import ProviderEvent
from app.routers import loop_webhooks
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from app.services.custody.loop_auth import build_loop_signature
from app.services.custody.loop_webhook import process_loop_collection_webhook
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


def _signed_headers(raw_payload: str, *, secret: str = "loop-signing-secret") -> dict[str, str]:
    timestamp = "20261002131500"
    nonce = "nonce-abc-123"
    signature = build_loop_signature(
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        payload=raw_payload,
    )
    return {
        "Content-Type": "application/json",
        "X-Loop-Timestamp": timestamp,
        "X-Loop-Nonce": nonce,
        "X-Loop-Signature": signature,
    }


def test_webhook_uses_inquiry_confirmation_instead_of_callback_claim() -> None:
    db = _build_db_session()
    try:
        payload = {
            "eventType": "collection.callback",
            "statusCode": "0",
            "statusDescription": "Prompt accepted",
            "transactionReference": "loop-ref-901",
            "phoneNumber": "+254712345678",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)

        inquiry_calls: list[str] = []

        def _inquiry_status(provider_reference: str) -> CollectionResult:
            inquiry_calls.append(provider_reference)
            return CollectionResult(
                outcome=CollectionOutcome.FAILED_DEFINITE,
                provider_reference=provider_reference,
                raw_status="declined",
                message="Insufficient funds.",
            )

        result = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_inquiry_status,
        )

        assert result.accepted is True
        assert result.duplicate is False
        assert result.signature_valid is True
        assert result.inquiry_outcome == CollectionOutcome.FAILED_DEFINITE
        assert inquiry_calls == ["loop-ref-901"]

        events = list(db.execute(select(ProviderEvent)).scalars().all())
        assert len(events) == 1
        event = events[0]
        assert event.dedupe_key == "collection:loop-ref-901"
        assert event.request_snapshot["signature_valid"] is True
        assert event.payload["phoneNumber"] == "***"
        assert event.response_snapshot["funding_confirmed"] is False
        assert event.response_snapshot["inquiry"]["outcome"] == "FAILED_DEFINITE"
    finally:
        db.close()


def test_webhook_processing_is_idempotent_for_same_reference() -> None:
    db = _build_db_session()
    try:
        payload = {
            "eventType": "collection.callback",
            "transactionReference": "loop-ref-902",
            "statusCode": "0",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)

        call_count = 0

        def _inquiry_status(provider_reference: str) -> CollectionResult:
            nonlocal call_count
            call_count += 1
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED,
                provider_reference=provider_reference,
                raw_status="completed",
                message="Settled.",
            )

        first = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_inquiry_status,
        )
        second = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_inquiry_status,
        )

        assert first.duplicate is False
        assert second.duplicate is True
        assert call_count == 1

        events = list(db.execute(select(ProviderEvent)).scalars().all())
        assert len(events) == 1
    finally:
        db.close()


def test_webhook_rejects_invalid_signature_without_inquiry() -> None:
    db = _build_db_session()
    try:
        payload = {
            "eventType": "collection.callback",
            "transactionReference": "loop-ref-903",
            "statusCode": "0",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)
        headers["X-Loop-Signature"] = "not-a-valid-signature"

        def _unexpected_inquiry(_provider_reference: str) -> CollectionResult:
            raise AssertionError("Inquiry should not run when signature is invalid.")

        result = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_unexpected_inquiry,
        )

        assert result.accepted is True
        assert result.signature_valid is False
        assert result.inquiry_outcome is None

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.request_snapshot["signature_valid"] is False
        assert event.response_snapshot["state"] == "ignored_invalid_signature"
    finally:
        db.close()


def test_webhook_handles_missing_provider_reference_without_inquiry() -> None:
    db = _build_db_session()
    try:
        payload = {
            "eventType": "collection.callback",
            "statusCode": "0",
            "statusDescription": "Accepted",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)

        def _unexpected_inquiry(_provider_reference: str) -> CollectionResult:
            raise AssertionError("Inquiry should not run when provider reference is missing.")

        result = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_unexpected_inquiry,
        )

        assert result.accepted is True
        assert result.signature_valid is True
        assert result.provider_reference is None
        assert result.inquiry_outcome is None

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.response_snapshot["state"] == "ignored_missing_provider_reference"
    finally:
        db.close()


def test_webhook_records_inquiry_failure_state() -> None:
    db = _build_db_session()
    try:
        payload = {
            "eventType": "collection.callback",
            "transactionReference": "loop-ref-905",
            "statusCode": "0",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)

        def _failing_inquiry(_provider_reference: str) -> CollectionResult:
            raise RuntimeError("sandbox inquiry temporarily unavailable")

        result = process_loop_collection_webhook(
            db,
            payload=payload,
            raw_payload=raw_payload,
            headers=headers,
            signing_secret="loop-signing-secret",
            inquiry_status_fn=_failing_inquiry,
        )

        assert result.accepted is True
        assert result.signature_valid is True
        assert result.provider_reference == "loop-ref-905"
        assert result.inquiry_outcome is None

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.response_snapshot["state"] == "inquiry_failed"
        assert "temporarily unavailable" in event.response_snapshot["message"]
    finally:
        db.close()


def test_loop_webhook_endpoint_processes_signed_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_db_session()
    fake_rail = SimpleNamespace(
        get_funding_status=lambda provider_reference: CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="completed",
            message="Settled.",
        )
    )
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = SimpleNamespace(collection_rails={"loop": fake_rail})
        monkeypatch.setattr(
            loop_webhooks,
            "get_settings",
            lambda: SimpleNamespace(loop_passkey="loop-signing-secret"),
        )
        app.dependency_overrides[get_db] = _override_get_db

        payload = {
            "eventType": "collection.callback",
            "transactionReference": "loop-ref-904",
            "statusCode": "0",
            "phoneNumber": "+254700000904",
        }
        raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        headers = _signed_headers(raw_payload)

        client = TestClient(app)
        response = client.post(
            "/webhooks/loop/collection",
            data=raw_payload,
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["duplicate"] is False
        assert body["signature_valid"] is True
        assert body["provider_reference"] == "loop-ref-904"
        assert body["inquiry_outcome"] == "SUCCEEDED"

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.dedupe_key == "collection:loop-ref-904"
        assert event.response_snapshot["funding_confirmed"] is True
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_loop_webhook_endpoint_returns_503_when_loop_rail_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_db_session()
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = SimpleNamespace(collection_rails={})
        monkeypatch.setattr(
            loop_webhooks,
            "get_settings",
            lambda: SimpleNamespace(loop_passkey="loop-signing-secret"),
        )
        app.dependency_overrides[get_db] = _override_get_db

        client = TestClient(app)
        response = client.post(
            "/webhooks/loop/collection",
            data='{"eventType":"collection.callback"}',
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 503
        assert response.json()["detail"] == "LOOP collection rail is unavailable."
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_loop_webhook_endpoint_accepts_malformed_json_payload_as_unknown_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_db_session()
    fake_rail = SimpleNamespace(
        get_funding_status=lambda provider_reference: CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="completed",
            message="Settled.",
        )
    )
    original_registry = getattr(app.state, "custody_registry", None)

    def _override_get_db() -> Iterator[Session]:
        yield db

    try:
        app.state.custody_registry = SimpleNamespace(collection_rails={"loop": fake_rail})
        monkeypatch.setattr(
            loop_webhooks,
            "get_settings",
            lambda: SimpleNamespace(loop_passkey="loop-signing-secret"),
        )
        app.dependency_overrides[get_db] = _override_get_db

        malformed_payload = '{"eventType":"collection.callback",'
        headers = _signed_headers(malformed_payload)

        client = TestClient(app)
        response = client.post(
            "/webhooks/loop/collection",
            data=malformed_payload,
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["signature_valid"] is True
        assert body["provider_reference"] is None
        assert body["inquiry_outcome"] is None

        event = db.execute(select(ProviderEvent)).scalar_one()
        assert event.response_snapshot["state"] == "ignored_missing_provider_reference"
    finally:
        if original_registry is None:
            if hasattr(app.state, "custody_registry"):
                delattr(app.state, "custody_registry")
        else:
            app.state.custody_registry = original_registry
        app.dependency_overrides.pop(get_db, None)
        db.close()
