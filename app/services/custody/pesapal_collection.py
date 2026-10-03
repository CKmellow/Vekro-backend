from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from app.services.custody.dto import CollectionResult, FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import (
    PesapalHttpResponse,
    PesapalHttpTransport,
    PesapalTokenManager,
    default_pesapal_http_transport,
)

PESAPAL_PROVIDER_NAME = "pesapal"

_FAILED_STATUS_VALUES = {
    "failed",
    "fail",
    "declined",
    "rejected",
    "invalid",
    "error",
    "cancelled",
    "canceled",
}
_TERMINAL_SUCCESS_STATES = {"completed", "paid", "successful", "succeeded"}
_TERMINAL_FAILED_STATES = {"failed", "cancelled", "canceled", "invalid", "rejected"}
_PENDING_STATES = {"pending", "processing", "queued", "waiting", "initiated"}


class PesapalCollectionRail:
    def __init__(
        self,
        *,
        base_url: str,
        callback_url: str,
        ipn_id: str,
        token_manager: PesapalTokenManager,
        transport: PesapalHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        now_fn: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._callback_url = callback_url.strip()
        self._ipn_id = ipn_id.strip()
        self._token_manager = token_manager
        self._transport = transport or default_pesapal_http_transport
        self._timeout_seconds = timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._logger = logger or logging.getLogger("app.custody.pesapal.collection")

        if not self._ipn_id:
            self._logger.warning(
                "pesapal_ipn_id_missing PESAPAL_ENABLED=true but PESAPAL_IPN_ID is blank; "
                "submit-order requests may fail until IPN registration is completed."
            )

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        if not self._ipn_id:
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=f"pesapal-order-{request.escrow_reference}",
                raw_status="missing_ipn_id",
                message="Pesapal IPN is not configured. Run the IPN registration action first.",
                metadata={
                    "order_tracking_id": None,
                    "redirect_url": None,
                },
            )

        payload = {
            "id": request.account_reference,
            "currency": request.currency,
            "amount": float(request.amount),
            "description": f"Escrow funding for {request.escrow_reference}",
            "callback_url": self._callback_url,
            "notification_id": self._ipn_id,
            "reference": request.escrow_reference,
            "billing_address": {
                "phone_number": request.phone_number,
            },
        }

        response = self._send(
            method="POST",
            endpoint="/api/Transactions/SubmitOrderRequest",
            payload=payload,
        )
        return self._parse_submit_response(response=response, fallback_reference=request.escrow_reference)

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        response = self._send(
            method="GET",
            endpoint="/api/Transactions/GetTransactionStatus",
            payload=None,
            query={"orderTrackingId": provider_reference},
        )
        return self._parse_status_response(
            response=response,
            fallback_reference=provider_reference,
        )

    def _send(
        self,
        *,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None,
        query: dict[str, str] | None = None,
    ) -> PesapalHttpResponse:
        token = self._token_manager.get_access_token()
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query)}"

        return self._transport(
            method,
            url,
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload,
            self._timeout_seconds,
        )

    def _parse_submit_response(
        self,
        *,
        response: PesapalHttpResponse,
        fallback_reference: str,
    ) -> CollectionResult:
        body = response.body if isinstance(response.body, dict) else {}
        status_value = _normalized_status(
            body,
            keys=("status", "status_code", "code", "message"),
        )

        order_tracking_id = str(
            body.get("order_tracking_id")
            or body.get("orderTrackingId")
            or body.get("tracking_id")
            or ""
        ).strip()
        redirect_url = str(
            body.get("redirect_url")
            or body.get("redirectUrl")
            or body.get("redirect_url_link")
            or ""
        ).strip()
        message = str(body.get("message") or body.get("error") or "").strip() or None

        provider_reference = order_tracking_id or f"pesapal-order-{fallback_reference}"

        metadata = {
            "order_tracking_id": order_tracking_id or provider_reference,
            "redirect_url": redirect_url or None,
        }

        if order_tracking_id and redirect_url:
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED,
                provider_reference=provider_reference,
                raw_status=status_value or "accepted",
                message=message,
                metadata=metadata,
            )

        if status_value in _FAILED_STATUS_VALUES:
            return CollectionResult(
                outcome=CollectionOutcome.FAILED_DEFINITE,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
                metadata=metadata,
            )

        if not order_tracking_id:
            self._log_malformed(context="submit_order", body=body, response=response)
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=status_value or "malformed_response",
                message="Pesapal submit-order response missing order tracking id.",
                metadata=metadata,
            )

        return CollectionResult(
            outcome=CollectionOutcome.UNKNOWN,
            provider_reference=provider_reference,
            raw_status=status_value or "pending",
            message=message,
            metadata=metadata,
        )

    def _parse_status_response(
        self,
        *,
        response: PesapalHttpResponse,
        fallback_reference: str,
    ) -> CollectionResult:
        body = response.body if isinstance(response.body, dict) else {}
        status_value = _normalized_status(
            body,
            keys=(
                "payment_status_description",
                "payment_status",
                "paymentStatus",
                "status",
            ),
        )
        provider_reference = str(
            body.get("order_tracking_id") or body.get("orderTrackingId") or fallback_reference
        ).strip()
        message = str(
            body.get("description")
            or body.get("status_description")
            or body.get("message")
            or ""
        ).strip() or None

        if not status_value:
            self._log_malformed(context="status_inquiry", body=body, response=response)
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status="malformed_response",
                message="Pesapal status response missing payment status.",
                metadata={
                    "order_tracking_id": provider_reference,
                    "redirect_url": None,
                },
            )

        if status_value in _TERMINAL_SUCCESS_STATES:
            outcome = CollectionOutcome.SUCCEEDED
        elif status_value in _TERMINAL_FAILED_STATES:
            outcome = CollectionOutcome.FAILED_DEFINITE
        elif status_value in _PENDING_STATES:
            outcome = CollectionOutcome.UNKNOWN
        else:
            outcome = CollectionOutcome.UNKNOWN

        return CollectionResult(
            outcome=outcome,
            provider_reference=provider_reference,
            raw_status=status_value,
            message=message,
            metadata={
                "order_tracking_id": provider_reference,
                "redirect_url": None,
            },
        )

    def _log_malformed(
        self,
        *,
        context: str,
        body: dict[str, Any],
        response: PesapalHttpResponse,
    ) -> None:
        self._logger.warning(
            "pesapal_%s_malformed_response status=%s body=%s",
            context,
            response.status_code,
            _redact_payload(body),
        )


def _normalized_status(body: dict[str, Any], *, keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in body:
            continue
        value = str(body.get(key) or "").strip().lower()
        if value:
            return value
    return ""


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
        "consumer_secret",
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


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)