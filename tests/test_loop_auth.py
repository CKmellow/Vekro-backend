from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from app.services.custody import loop_auth, loop_selftest
from app.services.custody.loop_auth import (
    LOOP_SIGNING_TEST_VECTOR,
    LoopHttpResponse,
    LoopTokenManager,
)
from app.services.custody.loop_selftest import run_rails_selftest


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def current(self) -> datetime:
        return self.now


def test_token_cache_refreshes_before_expiry_and_supports_endpoint_variants() -> None:
    calls: list[str] = []
    token_sequence = iter(["token-a", "token-b"])

    def fake_transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = headers
        _ = payload
        _ = timeout_seconds
        calls.append(url)

        if url == "https://sandbox.loop.example/oauth/token":
            return LoopHttpResponse(status_code=404, body={"error": "missing"}, text="missing")

        return LoopHttpResponse(
            status_code=200,
            body={
                "access_token": next(token_sequence),
                "expires_in": 40,
            },
            text="ok",
        )

    clock = _Clock(datetime(2026, 10, 2, 9, 0, tzinfo=UTC))
    manager = LoopTokenManager(
        base_url="https://sandbox.loop.example",
        client_id="loop-client",
        client_secret="loop-secret",
        transport=fake_transport,
        now_fn=clock.current,
        refresh_skew_seconds=30,
    )

    first = manager.get_access_token()
    second = manager.get_access_token()

    assert first == "token-a"
    assert second == "token-a"
    assert calls == [
        "https://sandbox.loop.example/oauth/token",
        "https://sandbox.loop.example/v1/oauth/token",
    ]

    # Token has 40s TTL and 30s refresh skew, so +15s must force a refresh.
    clock.now = clock.now + timedelta(seconds=15)
    third = manager.get_access_token()

    assert third == "token-b"
    assert calls == [
        "https://sandbox.loop.example/oauth/token",
        "https://sandbox.loop.example/v1/oauth/token",
        "https://sandbox.loop.example/oauth/token",
        "https://sandbox.loop.example/v1/oauth/token",
    ]


def test_hmac_signature_helper_matches_published_vector() -> None:
    actual = loop_auth.build_loop_signature(
        secret=LOOP_SIGNING_TEST_VECTOR.secret,
        timestamp=LOOP_SIGNING_TEST_VECTOR.timestamp,
        nonce=LOOP_SIGNING_TEST_VECTOR.nonce,
        payload=LOOP_SIGNING_TEST_VECTOR.payload,
    )
    assert actual == LOOP_SIGNING_TEST_VECTOR.expected_signature


def test_rails_selftest_fails_loudly_on_vector_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loop_selftest,
        "LOOP_SIGNING_TEST_VECTOR",
        replace(LOOP_SIGNING_TEST_VECTOR, expected_signature="deadbeef"),
    )

    with pytest.raises(RuntimeError, match="LOOP signing self-test failed"):
        run_rails_selftest()
