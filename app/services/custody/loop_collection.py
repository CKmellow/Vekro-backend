from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.services.custody.dto import CollectionResult, FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.loop_auth import (
    LoopHttpResponse,
    LoopHttpTransport,
    LoopTokenManager,
    build_loop_signature,
    default_loop_http_transport,
)

LOOP_PROVIDER_NAME = "loop"

_SUCCESS_STATUS_CODES = {"0", "00", "success", "accepted", "200"}
_FAILED_STATUS_CODES = {
    "1",
    "2",
    "3",
    "4",
    "failed",
    "failure",
    "declined",
    "error",
    "400",
    "401",
    "402",
    "403",
    "404",
    "500",
}
_TERMINAL_SUCCESS_STATES = {"completed", "settled", "success", "successful", "paid"}
_TERMINAL_FAILED_STATES = {"failed", "declined", "cancelled", "canceled", "reversed"}
_PENDING_STATES = {"pending", "processing", "queued", "in_progress", "initiated"}


class LoopCollectionRail:
    def __init__(
        self,
        *,
        base_url: str,
        shortcode: str,
        passkey: str,
        token_manager: LoopTokenManager,
        transport: LoopHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        now_fn: Callable[[], datetime] | None = None,
        nonce_fn: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._shortcode = shortcode.strip()
        self._passkey = passkey.strip()
        self._token_manager = token_manager
        self._transport = transport or default_loop_http_transport
        self._timeout_seconds = timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._nonce_fn = nonce_fn or (lambda: secrets.token_hex(8))
        self._logger = logger or logging.getLogger("app.custody.loop.collection")

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        payload = {
            "shortcode": self._shortcode,
            "phoneNumber": request.phone_number,
            "amount": str(request.amount),
            "currency": request.currency,
            "accountReference": request.account_reference,
            "escrowReference": request.escrow_reference,
        }

        response = self._send(
            endpoint="/collection/prompt",
            payload=payload,
        )
        return self._parse_prompt_response(
            response=response,
            fallback_reference=request.escrow_reference,
        )

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        response = self._send(
            endpoint="/collection/inquiry",
            payload={
                "shortcode": self._shortcode,
                "transactionReference": provider_reference,
            },
        )
        return self._parse_inquiry_response(
            response=response,
            fallback_reference=provider_reference,
        )

    def _send(self, *, endpoint: str, payload: dict[str, Any]) -> LoopHttpResponse:
        payload_text = _canonical_json(payload)
        token = self._token_manager.get_access_token()
        timestamp = self._now_fn().strftime("%Y%m%d%H%M%S")
        nonce = self._nonce_fn()
        signature = build_loop_signature(
            secret=self._passkey,
            timestamp=timestamp,
            nonce=nonce,
            payload=payload_text,
        )

        return self._transport(
            "POST",
            f"{self._base_url}/{endpoint.lstrip('/')}",
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Loop-Timestamp": timestamp,
                "X-Loop-Nonce": nonce,
                "X-Loop-Signature": signature,
            },
            payload,
            self._timeout_seconds,
        )

    def _parse_prompt_response(
        self,
        *,
        response: LoopHttpResponse,
        fallback_reference: str,
    ) -> CollectionResult:
        body = response.body if isinstance(response.body, dict) else {}
        status_code = str(body.get("statusCode") or "").strip().lower()
        provider_reference = str(
            body.get("transactionReference")
            or body.get("providerReference")
            or body.get("requestId")
            or f"loop-collect-{fallback_reference}"
        ).strip()
        message = str(body.get("message") or body.get("statusDescription") or "").strip() or None

        if not status_code:
            self._log_malformed(
                context="prompt",
                body=body,
                response=response,
            )
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status="malformed_response",
                message="Loop prompt response missing statusCode.",
            )

        if status_code in _SUCCESS_STATUS_CODES:
            outcome = CollectionOutcome.SUCCEEDED
        elif status_code in _FAILED_STATUS_CODES:
            outcome = CollectionOutcome.FAILED_DEFINITE
        else:
            outcome = CollectionOutcome.UNKNOWN

        return CollectionResult(
            outcome=outcome,
            provider_reference=provider_reference,
            raw_status=status_code,
            message=message,
        )

    def _parse_inquiry_response(
        self,
        *,
        response: LoopHttpResponse,
        fallback_reference: str,
    ) -> CollectionResult:
        body = response.body if isinstance(response.body, dict) else {}
        final_state = str(body.get("finalState") or body.get("final_state") or "").strip().lower()
        status_code = str(body.get("statusCode") or "").strip().lower()
        provider_reference = str(
            body.get("transactionReference") or body.get("providerReference") or fallback_reference
        ).strip()
        message = str(body.get("message") or body.get("statusDescription") or "").strip() or None

        if not final_state:
            self._log_malformed(
                context="inquiry",
                body=body,
                response=response,
            )
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=status_code or "malformed_response",
                message="Loop inquiry response missing finalState.",
            )

        if final_state in _TERMINAL_SUCCESS_STATES:
            outcome = CollectionOutcome.SUCCEEDED
        elif final_state in _TERMINAL_FAILED_STATES:
            outcome = CollectionOutcome.FAILED_DEFINITE
        elif final_state in _PENDING_STATES:
            outcome = CollectionOutcome.UNKNOWN
        else:
            outcome = CollectionOutcome.UNKNOWN

        raw_status = status_code or final_state
        return CollectionResult(
            outcome=outcome,
            provider_reference=provider_reference,
            raw_status=raw_status,
            message=message,
        )

    def _log_malformed(
        self,
        *,
        context: str,
        body: dict[str, Any],
        response: LoopHttpResponse,
    ) -> None:
        self._logger.warning(
            "loop_%s_malformed_response status=%s body=%s",
            context,
            response.status_code,
            _redact_payload(body),
        )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _redact_payload(value: Any) -> Any:
    sensitive_keys = {
        "phone",
        "phonenumber",
        "msisdn",
        "token",
        "authorization",
        "signature",
        "passkey",
        "client_secret",
    }
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = key.strip().lower().replace("-", "").replace("_", "")
            if key_lower in sensitive_keys:
                redacted[key] = "***"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        if value.startswith("+") and value[1:].isdigit() and len(value) >= 8:
            return "***"
        return value
    if isinstance(value, Decimal):
        return str(value)
    return value
