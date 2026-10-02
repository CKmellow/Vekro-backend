from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib import error, request


class LoopAuthError(Exception):
    pass


@dataclass(frozen=True)
class LoopHttpResponse:
    status_code: int
    body: dict[str, Any]
    text: str


LoopHttpTransport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, float],
    LoopHttpResponse,
]


@dataclass(frozen=True)
class LoopSigningVector:
    secret: str
    timestamp: str
    nonce: str
    payload: str
    expected_signature: str


LOOP_SIGNING_TEST_VECTOR = LoopSigningVector(
    secret="loop-signing-secret-test",
    timestamp="2026-10-01T09:30:00Z",
    nonce="nonce-123456",
    payload='{"amount":"100.00","currency":"KES","reference":"txn-123"}',
    expected_signature="08583de8174e4bf3b1d6372873e52fcd9cd7d6c047bab7ae2682198a9fe9f73b",
)


def build_loop_signature(*, secret: str, timestamp: str, nonce: str, payload: str) -> str:
    canonical = f"{timestamp}.{nonce}.{payload}".encode()
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class LoopTokenManager:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        token_url: str | None = None,
        token_endpoint_variants: tuple[str, ...] = (
            "/oauth/token",
            "/v1/oauth/token",
            "/api/oauth/token",
        ),
        refresh_skew_seconds: int = 30,
        transport: LoopHttpTransport | None = None,
        now_fn: Callable[[], datetime] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._token_url = token_url.strip() if token_url else ""
        self._token_endpoint_variants = token_endpoint_variants
        self._refresh_skew = timedelta(seconds=max(0, refresh_skew_seconds))
        self._transport = transport or _default_http_transport
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._timeout_seconds = timeout_seconds

        self._cached_token = ""
        self._expires_at: datetime | None = None

    def get_access_token(self) -> str:
        if self._cached_token and not self._is_expiring_soon():
            return self._cached_token

        self._refresh_token()
        return self._cached_token

    def _is_expiring_soon(self) -> bool:
        if self._expires_at is None:
            return True
        return self._now_fn() + self._refresh_skew >= self._expires_at

    def _refresh_token(self) -> None:
        if not self._base_url:
            raise LoopAuthError("LOOP base URL is required for token refresh.")
        if not self._client_id or not self._client_secret:
            raise LoopAuthError("LOOP client credentials are required for token refresh.")

        token_errors: list[str] = []
        for candidate_url in self._candidate_token_urls():
            try:
                response = self._transport(
                    "POST",
                    candidate_url,
                    {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    {
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "client_credentials",
                    },
                    self._timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover
                token_errors.append(f"{candidate_url}: {exc}")
                continue

            if response.status_code < 200 or response.status_code >= 300:
                token_errors.append(
                    f"{candidate_url}: HTTP {response.status_code} {response.text[:180]}"
                )
                continue

            token, expires_in = _extract_token_payload(response.body)
            if not token:
                token_errors.append(f"{candidate_url}: missing access token")
                continue

            now = self._now_fn()
            self._cached_token = token
            self._expires_at = now + timedelta(seconds=max(1, expires_in))
            return

        detail = " | ".join(token_errors) if token_errors else "no token endpoint attempts"
        raise LoopAuthError(f"Failed to obtain LOOP access token. {detail}")

    def _candidate_token_urls(self) -> list[str]:
        if self._token_url:
            return [_absolute_url(self._base_url, self._token_url)]
        return [_absolute_url(self._base_url, path) for path in self._token_endpoint_variants]


def _absolute_url(base_url: str, path_or_url: str) -> str:
    value = path_or_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{base_url}/{value.lstrip('/')}"


def _extract_token_payload(body: dict[str, Any]) -> tuple[str, int]:
    direct_token = body.get("access_token") or body.get("token")
    nested = body.get("data")

    token = str(direct_token or "").strip()
    if not token and isinstance(nested, dict):
        token = str(nested.get("access_token") or nested.get("token") or "").strip()

    expires_raw: Any = body.get("expires_in")
    if expires_raw is None:
        expires_raw = body.get("expiresIn")
    if expires_raw is None and isinstance(nested, dict):
        expires_raw = nested.get("expires_in") or nested.get("expiresIn")

    try:
        expires_in = int(expires_raw) if expires_raw is not None else 3600
    except (TypeError, ValueError):
        expires_in = 3600

    return token, max(1, expires_in)


def _default_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> LoopHttpResponse:
    raw_body: bytes | None = None
    if payload is not None:
        raw_body = json.dumps(payload).encode("utf-8")

    request_obj = request.Request(url=url, method=method.upper(), headers=headers, data=raw_body)
    try:
        with request.urlopen(request_obj, timeout=timeout_seconds) as response:
            text_body = response.read().decode("utf-8")
            return LoopHttpResponse(
                status_code=response.status,
                body=_safe_json_parse(text_body),
                text=text_body,
            )
    except error.HTTPError as exc:
        text_body = exc.read().decode("utf-8")
        return LoopHttpResponse(
            status_code=exc.code,
            body=_safe_json_parse(text_body),
            text=text_body,
        )
    except error.URLError as exc:  # pragma: no cover
        raise LoopAuthError(f"Token transport error for {url}: {exc.reason}") from exc


default_loop_http_transport = _default_http_transport


def _safe_json_parse(text_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
