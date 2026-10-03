from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.services.custody.pesapal_auth import (
    PesapalAuthError,
    PesapalHttpResponse,
    PesapalTokenManager,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pesapal"


def _fixture(name: str) -> dict[str, str]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def current(self) -> datetime:
        return self.now


def test_auth_request_shape_and_proactive_refresh() -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
    token_sequence = iter(["token-a", "token-b"])
    clock = _Clock(datetime(2026, 10, 3, 9, 0, tzinfo=UTC))

    def _transport(method, url, headers, payload, timeout_seconds):
        _ = timeout_seconds
        calls.append((method, url, headers, payload))
        body = {
            "token": next(token_sequence),
            "expires_in": 300,
        }
        return PesapalHttpResponse(status_code=200, body=body, text="ok")

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
        now_fn=clock.current,
        refresh_skew_seconds=60,
    )

    first = manager.get_access_token()
    second = manager.get_access_token()

    assert first == "token-a"
    assert second == "token-a"
    assert len(calls) == 1

    method, url, headers, payload = calls[0]
    assert method == "POST"
    assert url == "https://cybqa.pesapal.com/pesapalv3/api/Auth/RequestToken"
    assert headers["Content-Type"] == "application/json"
    assert payload == {
        "consumer_key": "pesapal-key",
        "consumer_secret": "pesapal-secret",
    }

    # Default 60-second skew should force refresh once <= 60 seconds remain.
    clock.now = clock.now + timedelta(minutes=4, seconds=1)
    third = manager.get_access_token()

    assert third == "token-b"
    assert len(calls) == 2


def test_nested_token_and_iso_expiry_are_parsed() -> None:
    calls = 0
    clock = _Clock(datetime(2026, 10, 3, 10, 0, tzinfo=UTC))

    def _transport(method, url, headers, payload, timeout_seconds):
        nonlocal calls
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        calls += 1
        return PesapalHttpResponse(
            status_code=200,
            body={
                "data": {
                    "access_token": "nested-token",
                    "expiryDate": "2026-10-03T10:05:00Z",
                }
            },
            text="ok",
        )

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
        now_fn=clock.current,
    )

    assert manager.get_access_token() == "nested-token"
    clock.now = clock.now + timedelta(minutes=3)
    assert manager.get_access_token() == "nested-token"
    assert calls == 1


def test_auth_contract_fixture_payload_is_parsed() -> None:
    fixture_body = _fixture("auth_request_token_success.json")

    def _transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return PesapalHttpResponse(status_code=200, body=fixture_body, text="ok")

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
    )

    assert manager.get_access_token() == "fixture-pesapal-token"


def test_http_error_raises_clear_exception() -> None:
    def _transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return PesapalHttpResponse(status_code=401, body={"error": "unauthorized"}, text="401")

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
    )

    with pytest.raises(PesapalAuthError, match="HTTP 401"):
        manager.get_access_token()


def test_missing_token_field_raises_exception() -> None:
    def _transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return PesapalHttpResponse(status_code=200, body={"status": "ok"}, text="ok")

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
    )

    with pytest.raises(PesapalAuthError, match="token field missing"):
        manager.get_access_token()


def test_malformed_expiry_falls_back_to_default_ttl() -> None:
    token_sequence = iter(["token-a", "token-b"])
    clock = _Clock(datetime(2026, 10, 3, 11, 0, tzinfo=UTC))

    def _transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return PesapalHttpResponse(
            status_code=200,
            body={"token": next(token_sequence), "expiryDate": "not-a-date"},
            text="ok",
        )

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        transport=_transport,
        now_fn=clock.current,
        refresh_skew_seconds=60,
        default_ttl_seconds=300,
    )

    assert manager.get_access_token() == "token-a"
    clock.now = clock.now + timedelta(minutes=4, seconds=1)
    assert manager.get_access_token() == "token-b"