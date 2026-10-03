from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib import error, request


class PesapalAuthError(Exception):
    pass


@dataclass(frozen=True)
class PesapalHttpResponse:
    status_code: int
    body: dict[str, Any]
    text: str


PesapalHttpTransport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, float],
    PesapalHttpResponse,
]


class PesapalTokenManager:
    def __init__(
        self,
        *,
        base_url: str,
        consumer_key: str,
        consumer_secret: str,
        token_path: str = "/api/Auth/RequestToken",
        refresh_skew_seconds: int = 60,
        default_ttl_seconds: int = 300,
        transport: PesapalHttpTransport | None = None,
        now_fn: Callable[[], datetime] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._consumer_key = consumer_key.strip()
        self._consumer_secret = consumer_secret.strip()
        self._token_path = token_path.strip() or "/api/Auth/RequestToken"
        self._refresh_skew = timedelta(seconds=max(0, refresh_skew_seconds))
        self._default_ttl_seconds = max(1, default_ttl_seconds)
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
            raise PesapalAuthError("Pesapal base URL is required for token refresh.")
        if not self._consumer_key or not self._consumer_secret:
            raise PesapalAuthError("Pesapal consumer credentials are required for token refresh.")

        token_url = _absolute_url(self._base_url, self._token_path)
        try:
            response = self._transport(
                "POST",
                token_url,
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                {
                    "consumer_key": self._consumer_key,
                    "consumer_secret": self._consumer_secret,
                },
                self._timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover
            raise PesapalAuthError(f"Pesapal auth transport error: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise PesapalAuthError(
                "Failed to obtain Pesapal access token. "
                f"HTTP {response.status_code} {response.text[:180]}"
            )

        now = self._now_fn()
        token = _extract_token(response.body)
        if not token:
            raise PesapalAuthError("Failed to obtain Pesapal access token: token field missing.")

        expires_at = _extract_expiry(
            response.body,
            now=now,
            default_ttl_seconds=self._default_ttl_seconds,
        )
        self._cached_token = token
        self._expires_at = expires_at


def _absolute_url(base_url: str, path_or_url: str) -> str:
    value = path_or_url.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{base_url}/{value.lstrip('/')}"


def _extract_token(body: dict[str, Any]) -> str:
    token_keys = (
        "token",
        "access_token",
        "accessToken",
        "authorization_token",
        "authorizationToken",
        "bearer_token",
    )
    nested_keys = ("data", "result", "response")

    token = _extract_first_text(body, token_keys)
    if token:
        return token

    for nested_key in nested_keys:
        nested = body.get(nested_key)
        if isinstance(nested, dict):
            token = _extract_first_text(nested, token_keys)
            if token:
                return token
    return ""


def _extract_expiry(
    body: dict[str, Any],
    *,
    now: datetime,
    default_ttl_seconds: int,
) -> datetime:
    ttl_keys = ("expires_in", "expiresIn", "expiry_in", "expiryIn", "expires")
    datetime_keys = (
        "expiryDate",
        "expiry_date",
        "expires_at",
        "expiresAt",
        "expiry",
    )
    nested_keys = ("data", "result", "response")

    ttl_value = _extract_first_value(body, ttl_keys)
    if ttl_value is None:
        ttl_value = _extract_nested_value(body, nested_keys, ttl_keys)
    if ttl_value is not None:
        ttl_seconds = _parse_ttl_seconds(ttl_value)
        if ttl_seconds is not None:
            return now + timedelta(seconds=max(1, ttl_seconds))

    expiry_text = _extract_first_text(body, datetime_keys)
    if not expiry_text:
        expiry_text = _extract_nested_text(body, nested_keys, datetime_keys)
    if expiry_text:
        parsed = _parse_datetime(expiry_text)
        if parsed is not None:
            return parsed

    return now + timedelta(seconds=default_ttl_seconds)


def _parse_ttl_seconds(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_datetime(value: str) -> datetime | None:
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    value = _extract_first_value(payload, keys)
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _extract_first_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _extract_nested_value(
    payload: dict[str, Any],
    nested_keys: tuple[str, ...],
    keys: tuple[str, ...],
) -> Any:
    for nested_key in nested_keys:
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            value = _extract_first_value(nested, keys)
            if value is not None:
                return value
    return None


def _extract_nested_text(
    payload: dict[str, Any],
    nested_keys: tuple[str, ...],
    keys: tuple[str, ...],
) -> str:
    for nested_key in nested_keys:
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            text = _extract_first_text(nested, keys)
            if text:
                return text
    return ""


def _default_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> PesapalHttpResponse:
    raw_body: bytes | None = None
    if payload is not None:
        raw_body = json.dumps(payload).encode("utf-8")

    request_obj = request.Request(url=url, method=method.upper(), headers=headers, data=raw_body)
    try:
        with request.urlopen(request_obj, timeout=timeout_seconds) as response:
            text_body = response.read().decode("utf-8")
            return PesapalHttpResponse(
                status_code=response.status,
                body=_safe_json_parse(text_body),
                text=text_body,
            )
    except error.HTTPError as exc:
        text_body = exc.read().decode("utf-8")
        return PesapalHttpResponse(
            status_code=exc.code,
            body=_safe_json_parse(text_body),
            text=text_body,
        )
    except error.URLError as exc:  # pragma: no cover
        raise PesapalAuthError(f"Pesapal auth transport error for {url}: {exc.reason}") from exc


default_pesapal_http_transport = _default_http_transport


def _safe_json_parse(text_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed