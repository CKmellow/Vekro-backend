"""Real HTTP transport for the eConfirm custody adapter (M20 / E2).

A thin, injectable httpx client that implements the existing
``EconfirmHttpTransport`` callable contract
``(method, url, headers, payload, timeout_seconds) -> EconfirmHttpResponse``
so the M17 mocked transport and its unit tests keep working unchanged.

Responsibilities (E2 only):
    * send requests with a configurable browser User-Agent (eConfirm's Cloudflare
      WAF 403-bans stdlib User-Agents, Error 1010);
    * per-request request id and optional correlation id (read from an
      ``X-Correlation-Id`` request header) in structured, redacted logs;
    * map transport/response conditions to the M10 taxonomy signal so the caller
      can drive the circuit breaker and fallback (A8): connection/timeout, 5xx,
      429, a Cloudflare block, and non-JSON bodies are infrastructure failures;
    * never log the API key, bearer token, confirmation codes or phone numbers.

The taxonomy *outcome* (SUCCEEDED / FAILED_DEFINITE / UNKNOWN) for business
statuses is still decided by the provider/adapter (E3+). This layer only
distinguishes transport health so a validation 4xx never opens the breaker while
a timeout or Cloudflare block does.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2 as httpx

from app.services.custody.econfirm_provider import EconfirmHttpResponse
from app.services.custody.redaction import redact_structure, safe_error_message

#: Failure classifications that should count against the circuit breaker.
INFRA_FAILURE_KINDS = frozenset(
    {"timeout", "connection", "http_5xx", "http_429", "cloudflare", "non_json"}
)


@dataclass(frozen=True)
class EconfirmRequestOutcome:
    """Transport-health summary passed to the optional breaker hook."""

    is_success: bool
    status_code: int
    failure_kind: str | None
    cloudflare_blocked: bool
    request_id: str
    correlation_id: str | None
    error_code: str | None = None


BreakerHook = Callable[[EconfirmRequestOutcome], None]


class EconfirmHttpxTransport:
    """Callable httpx transport matching ``EconfirmHttpTransport``."""

    def __init__(
        self,
        *,
        user_agent: str,
        logger: logging.Logger | None = None,
        httpx_transport: Any | None = None,
        breaker_hook: BreakerHook | None = None,
    ) -> None:
        self._user_agent = user_agent.strip() or "VekroBackend/1.0"
        self._logger = logger or logging.getLogger("app.custody.econfirm.transport")
        # Injected for tests (e.g. httpx2.MockTransport); None uses the real network.
        self._httpx_transport = httpx_transport
        self._breaker_hook = breaker_hook

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> EconfirmHttpResponse:
        request_id = uuid4().hex[:16]
        send_headers: dict[str, str] = dict(headers or {})
        send_headers.setdefault("User-Agent", self._user_agent)
        send_headers.setdefault("X-Request-Id", request_id)
        correlation_id = send_headers.get("X-Correlation-Id")
        path = urlsplit(url).path or url
        started = perf_counter()

        try:
            with self._make_client(timeout_seconds) as client:
                if payload is None:
                    response = client.request(method.upper(), url, headers=send_headers)
                else:
                    response = client.request(
                        method.upper(), url, headers=send_headers, json=payload
                    )
        except httpx.TimeoutException as exc:
            return self._transport_error(
                "timeout", exc, request_id, correlation_id, method, path, payload, started
            )
        except httpx.RequestError as exc:
            return self._transport_error(
                "connection", exc, request_id, correlation_id, method, path, payload, started
            )

        text = response.text
        parsed = _safe_json(text)
        non_json = not isinstance(parsed, dict)
        body: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        cloudflare_blocked = _is_cloudflare_block(response.status_code, body, text)
        failure_kind = _classify(response.status_code, non_json, cloudflare_blocked)
        duration_ms = round((perf_counter() - started) * 1000, 1)

        outcome = EconfirmRequestOutcome(
            is_success=failure_kind is None,
            status_code=response.status_code,
            failure_kind=failure_kind,
            cloudflare_blocked=cloudflare_blocked,
            request_id=request_id,
            correlation_id=correlation_id,
            error_code=_extract_error_code(body),
        )
        self._log(method, path, outcome, duration_ms, payload)
        self._notify(outcome)

        return EconfirmHttpResponse(
            status_code=response.status_code,
            body=body,
            text=text,
            non_json=non_json,
            cloudflare_blocked=cloudflare_blocked,
        )

    # -- internals -------------------------------------------------------------
    def _make_client(self, timeout_seconds: float) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if self._httpx_transport is not None:
            kwargs["transport"] = self._httpx_transport
        return httpx.Client(**kwargs)

    def _transport_error(
        self,
        failure_kind: str,
        exc: BaseException,
        request_id: str,
        correlation_id: str | None,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        started: float,
    ) -> EconfirmHttpResponse:
        safe = safe_error_message(exc)
        duration_ms = round((perf_counter() - started) * 1000, 1)
        outcome = EconfirmRequestOutcome(
            is_success=False,
            status_code=0,
            failure_kind=failure_kind,
            cloudflare_blocked=False,
            request_id=request_id,
            correlation_id=correlation_id,
            error_code=failure_kind,
        )
        self._log(method, path, outcome, duration_ms, payload)
        self._notify(outcome)
        # status_code=0 + status "transport_error" preserves the contract the
        # provider relies on to map transport failures to UNKNOWN.
        return EconfirmHttpResponse(
            status_code=0,
            body={"status": "transport_error", "message": safe},
            text=safe,
        )

    def _log(
        self,
        method: str,
        path: str,
        outcome: EconfirmRequestOutcome,
        duration_ms: float,
        payload: dict[str, Any] | None,
    ) -> None:
        detail = (
            "econfirm_http method=%s path=%s status=%s outcome=%s cloudflare=%s "
            "request_id=%s correlation_id=%s duration_ms=%s payload=%s"
        )
        args = (
            method.upper(),
            path,
            outcome.status_code,
            "ok" if outcome.is_success else (outcome.failure_kind or "failure"),
            outcome.cloudflare_blocked,
            outcome.request_id,
            outcome.correlation_id or "-",
            duration_ms,
            redact_structure(payload) if payload is not None else None,
        )
        if outcome.is_success:
            self._logger.info(detail, *args)
        else:
            self._logger.warning(detail, *args)

    def _notify(self, outcome: EconfirmRequestOutcome) -> None:
        if self._breaker_hook is None:
            return
        try:
            self._breaker_hook(outcome)
        except Exception:  # pragma: no cover - a breaker hook must never break a call
            self._logger.exception("econfirm breaker hook failed request_id=%s", outcome.request_id)


def _safe_json(text_value: str) -> Any:
    try:
        return json.loads(text_value)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_cloudflare_block(status_code: int, body: dict[str, Any], text: str) -> bool:
    if status_code != 403:
        return False
    if body.get("cloudflare_error") is True:
        return True
    try:
        if int(body.get("error_code", 0)) == 1010:
            return True
    except (TypeError, ValueError):
        pass
    lowered = text.lower()
    return "cloudflare" in lowered or "error 1010" in lowered


def _classify(status_code: int, non_json: bool, cloudflare_blocked: bool) -> str | None:
    """Return an infra failure kind, or None when the exchange is healthy.

    A 4xx validation rejection is a healthy exchange for breaker purposes: the
    request reached eConfirm and was answered. Only infrastructure problems
    (timeouts, 5xx, 429, Cloudflare, unexpected non-JSON) count against health.
    """
    if cloudflare_blocked:
        return "cloudflare"
    if status_code == 429:
        return "http_429"
    if status_code >= 500:
        return "http_5xx"
    if non_json and status_code < 400:
        return "non_json"
    return None


def _extract_error_code(body: dict[str, Any]) -> str | None:
    for key in ("error_code", "error_name", "code"):
        value = body.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def build_econfirm_transport(
    settings: Any,
    *,
    breaker_hook: BreakerHook | None = None,
    logger: logging.Logger | None = None,
) -> EconfirmHttpxTransport:
    """Construct the real transport from settings (used at the composition root)."""
    user_agent = getattr(
        settings,
        "econfirm_user_agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    return EconfirmHttpxTransport(
        user_agent=user_agent,
        logger=logger,
        breaker_hook=breaker_hook,
    )
