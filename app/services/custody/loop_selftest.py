from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime, timedelta

from app.services.custody.loop_auth import LOOP_SIGNING_TEST_VECTOR, build_loop_signature
from app.services.custody.pesapal_auth import PesapalHttpResponse, PesapalTokenManager


def _truthy_env(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _run_pesapal_fixture_auth_selftest() -> None:
    observed_calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
    clock = {"now": datetime(2026, 10, 3, 9, 0, tzinfo=UTC)}
    token_sequence = iter(["fixture-token-a", "fixture-token-b"])

    def _now() -> datetime:
        return clock["now"]

    def _transport(method, url, headers, payload, timeout_seconds):
        _ = timeout_seconds
        observed_calls.append((method, url, headers, payload))
        body = {
            "token": next(token_sequence),
            "expiryDate": (_now() + timedelta(minutes=5)).isoformat(),
        }
        return PesapalHttpResponse(status_code=200, body=body, text="ok")

    manager = PesapalTokenManager(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="fixture-consumer-key",
        consumer_secret="fixture-consumer-secret",
        transport=_transport,
        now_fn=_now,
        refresh_skew_seconds=60,
    )

    first = manager.get_access_token()
    second = manager.get_access_token()
    if first != "fixture-token-a" or second != "fixture-token-a":
        raise RuntimeError("Pesapal fixture auth self-test failed: token cache behavior mismatch.")

    clock["now"] = clock["now"] + timedelta(minutes=4, seconds=10)
    third = manager.get_access_token()
    if third != "fixture-token-b":
        raise RuntimeError("Pesapal fixture auth self-test failed: proactive refresh mismatch.")

    if len(observed_calls) != 2:
        raise RuntimeError(
            "Pesapal fixture auth self-test failed: expected 2 auth calls "
            f"(initial + proactive refresh), got {len(observed_calls)}."
        )

    method, url, headers, payload = observed_calls[0]
    if method != "POST":
        raise RuntimeError("Pesapal fixture auth self-test failed: auth method must be POST.")
    if not url.endswith("/api/Auth/RequestToken"):
        raise RuntimeError("Pesapal fixture auth self-test failed: auth endpoint path mismatch.")
    if headers.get("Content-Type") != "application/json":
        raise RuntimeError(
            "Pesapal fixture auth self-test failed: missing application/json request header."
        )
    if payload.get("consumer_key") != "fixture-consumer-key":
        raise RuntimeError("Pesapal fixture auth self-test failed: consumer_key payload mismatch.")
    if payload.get("consumer_secret") != "fixture-consumer-secret":
        raise RuntimeError("Pesapal fixture auth self-test failed: consumer_secret payload mismatch.")


def _run_optional_pesapal_live_auth_selftest() -> None:
    if not _truthy_env("RUN_PESAPAL_LIVE_AUTH_SELFTEST"):
        return

    base_url = os.getenv("PESAPAL_BASE_URL", "").strip()
    consumer_key = os.getenv("PESAPAL_CONSUMER_KEY", "").strip()
    consumer_secret = os.getenv("PESAPAL_CONSUMER_SECRET", "").strip()

    missing: list[str] = []
    if not base_url:
        missing.append("PESAPAL_BASE_URL")
    if not consumer_key:
        missing.append("PESAPAL_CONSUMER_KEY")
    if not consumer_secret:
        missing.append("PESAPAL_CONSUMER_SECRET")

    if missing:
        raise RuntimeError(
            "Pesapal live auth self-test requested but required env values are missing: "
            + ", ".join(missing)
        )

    manager = PesapalTokenManager(
        base_url=base_url,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
    )
    token = manager.get_access_token()
    if not token:
        raise RuntimeError("Pesapal live auth self-test returned an empty token.")


def run_rails_selftest() -> None:
    vector = LOOP_SIGNING_TEST_VECTOR
    actual = build_loop_signature(
        secret=vector.secret,
        timestamp=vector.timestamp,
        nonce=vector.nonce,
        payload=vector.payload,
    )
    if not hmac.compare_digest(actual, vector.expected_signature):
        raise RuntimeError(
            "LOOP signing self-test failed: expected " f"{vector.expected_signature}, got {actual}."
        )

    _run_pesapal_fixture_auth_selftest()
    _run_optional_pesapal_live_auth_selftest()


def main() -> None:
    run_rails_selftest()
    print("Rails self-test passed (LOOP signing + Pesapal auth).")


if __name__ == "__main__":
    main()
