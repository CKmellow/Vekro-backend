from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib import error, request
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from app.services.custody.dto import (
    CollectionResult,
    CustodyCapabilities,
    EscrowRecord,
    EscrowStatusResult,
    FundingRequest,
    OpenEscrowRequest,
    PayoutRequest,
    PayoutResult,
)
from app.services.custody.enums import CollectionOutcome, PayoutOutcome

ECONFIRM_PROVIDER_NAME = "econfirm"

_SUCCESS_STATUSES = frozenset(
    {
        "ok",
        "accepted",
        "succeeded",
        "success",
        "completed",
        "settled",
        "funded",
        "released",
        "reversed",
        "refunded",
        "duplicate",
        "already_exists",
        "already_processed",
        "200",
        "201",
        "202",
    }
)
_FAILED_STATUSES = frozenset(
    {
        "failed",
        "failure",
        "declined",
        "rejected",
        "cancelled",
        "canceled",
        "error",
        "unauthorized",
        "forbidden",
        "invalid",
        "insufficient_funds",
        "400",
        "401",
        "403",
        "404",
        "409",
        "422",
        "500",
    }
)
_PENDING_STATUSES = frozenset(
    {
        "pending",
        "processing",
        "queued",
        "in_progress",
        "initiated",
        "awaiting_confirmation",
    }
)


@dataclass(frozen=True)
class EconfirmHttpResponse:
    status_code: int
    body: dict[str, Any]
    text: str


EconfirmHttpTransport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, float],
    EconfirmHttpResponse,
]


class EconfirmCustodyProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        transport: EconfirmHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._transport = transport or default_econfirm_http_transport
        self._timeout_seconds = timeout_seconds
        self._logger = logger or logging.getLogger("app.custody.econfirm")

        self._open_escrow_cache: dict[str, EscrowRecord] = {}
        self._funding_cache: dict[str, CollectionResult] = {}
        self._payout_cache: dict[str, PayoutResult] = {}

    def capabilities(self) -> CustodyCapabilities:
        return CustodyCapabilities(
            holds_funds_structurally=True,
            supports_split_payout=False,
            supports_partial_release=False,
            supports_webhook_auth=True,
        )

    def open_escrow(self, request_model: OpenEscrowRequest) -> EscrowRecord:
        cached = self._open_escrow_cache.get(request_model.transaction_id)
        if cached is not None:
            return cached

        payload = {
            "transaction_id": request_model.transaction_id,
            "buyer_id": request_model.buyer_id,
            "seller_id": request_model.seller_id,
            "amount": str(request_model.amount),
            "currency": request_model.currency,
            "idempotency_key": self._build_reference(
                "open",
                request_model.transaction_id,
            ),
        }
        response = self._send("POST", "/v1/escrows/open", payload)
        body = response.body if isinstance(response.body, dict) else {}

        status_value = _normalized_status(body)
        if status_value in _FAILED_STATUSES:
            message = _extract_message(body) or "eConfirm rejected escrow opening request."
            raise RuntimeError(message)

        escrow_reference = _extract_first_text(
            body,
            (
                "escrow_reference",
                "escrowReference",
                "escrow_id",
                "escrowId",
                "id",
            ),
        )
        if not escrow_reference:
            escrow_reference = self._build_reference("escrow", request_model.transaction_id)

        record = EscrowRecord(
            escrow_reference=escrow_reference,
            transaction_id=request_model.transaction_id,
            amount=request_model.amount,
            currency=request_model.currency,
        )
        self._open_escrow_cache[request_model.transaction_id] = record
        return record

    def request_funding(self, request_model: FundingRequest) -> CollectionResult:
        cache_key = (
            f"fund:{request_model.escrow_reference}:{request_model.account_reference}:"
            f"{request_model.amount}:{request_model.currency}"
        )
        cached = self._funding_cache.get(cache_key)
        if cached is not None:
            return cached

        payload = {
            "amount": str(request_model.amount),
            "currency": request_model.currency,
            "phone_number": request_model.phone_number,
            "account_reference": request_model.account_reference,
            "idempotency_key": self._build_reference("fund", cache_key),
        }
        endpoint = f"/v1/escrows/{quote(request_model.escrow_reference, safe='')}/fund"
        response = self._send("POST", endpoint, payload)

        result = self._parse_collection_result(
            response=response,
            fallback_reference=self._build_reference("fund", cache_key),
            malformed_message="eConfirm funding response is missing status.",
        )
        self._funding_cache[cache_key] = result
        return result

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        endpoint = f"/v1/fundings/{quote(provider_reference, safe='')}"
        response = self._send("GET", endpoint, None)
        return self._parse_collection_result(
            response=response,
            fallback_reference=provider_reference,
            malformed_message="eConfirm funding-status response is missing status.",
        )

    def get_status(self, escrow_reference: str) -> EscrowStatusResult:
        endpoint = f"/v1/escrows/{quote(escrow_reference, safe='')}"
        response = self._send("GET", endpoint, None)
        body = response.body if isinstance(response.body, dict) else {}

        funded_amount = _extract_decimal(
            body,
            ("funded_amount", "fundedAmount", "funded"),
            default=Decimal("0.00"),
        )
        released_amount = _extract_decimal(
            body,
            ("released_amount", "releasedAmount", "released"),
            default=Decimal("0.00"),
        )
        refunded_amount = _extract_decimal(
            body,
            ("refunded_amount", "refundedAmount", "refunded", "reversed_amount"),
            default=Decimal("0.00"),
        )

        return EscrowStatusResult(
            escrow_reference=escrow_reference,
            funded_amount=funded_amount,
            released_amount=released_amount,
            refunded_amount=refunded_amount,
        )

    def request_payout(self, request_model: PayoutRequest) -> PayoutResult:
        inferred_kind = "refund" if "refund" in request_model.purpose.lower() else "release"
        return self._submit_payout(
            request_model,
            payout_kind=inferred_kind,
            cache_namespace="rail",
        )

    def get_payout_status(self, provider_reference: str) -> PayoutResult:
        endpoint = f"/v1/payouts/{quote(provider_reference, safe='')}"
        response = self._send("GET", endpoint, None)
        return self._parse_payout_result(
            response=response,
            fallback_reference=provider_reference,
            malformed_message="eConfirm payout-status response is missing status.",
        )

    def release(self, request_model: PayoutRequest) -> PayoutResult:
        return self._submit_payout(
            request_model,
            payout_kind="release",
            cache_namespace="provider",
        )

    def refund(self, request_model: PayoutRequest) -> PayoutResult:
        return self._submit_payout(
            request_model,
            payout_kind="refund",
            cache_namespace="provider",
        )

    def _submit_payout(
        self,
        request_model: PayoutRequest,
        *,
        payout_kind: str,
        cache_namespace: str,
    ) -> PayoutResult:
        cache_key = (
            f"{cache_namespace}:{payout_kind}:{request_model.escrow_reference}:{request_model.purpose}:"
            f"{request_model.amount}:{request_model.currency}:{request_model.destination_phone}"
        )
        cached = self._payout_cache.get(cache_key)
        if cached is not None:
            return cached

        endpoint_suffix = "reversals" if payout_kind == "refund" else "releases"
        endpoint = f"/v1/escrows/{quote(request_model.escrow_reference, safe='')}/{endpoint_suffix}"
        payload = {
            "amount": str(request_model.amount),
            "currency": request_model.currency,
            "destination_phone": request_model.destination_phone,
            "purpose": request_model.purpose,
            "idempotency_key": self._build_reference(payout_kind, cache_key),
        }
        response = self._send("POST", endpoint, payload)
        result = self._parse_payout_result(
            response=response,
            fallback_reference=self._build_reference(payout_kind, cache_key),
            malformed_message=f"eConfirm {payout_kind} response is missing status.",
        )
        self._payout_cache[cache_key] = result
        return result

    def _send(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None,
    ) -> EconfirmHttpResponse:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"

        try:
            return self._transport(
                method,
                url,
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Econfirm-Api-Key": self._api_key,
                    "X-Econfirm-Api-Secret": self._api_secret,
                },
                payload,
                self._timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - exercised by classification tests
            message = str(exc)
            self._logger.warning(
                "econfirm_transport_error endpoint=%s payload=%s error=%s",
                endpoint,
                _redact_payload(payload or {}),
                message,
            )
            return EconfirmHttpResponse(
                status_code=0,
                body={
                    "status": "transport_error",
                    "message": message,
                },
                text=message,
            )

    def _parse_collection_result(
        self,
        *,
        response: EconfirmHttpResponse,
        fallback_reference: str,
        malformed_message: str,
    ) -> CollectionResult:
        body = response.body if isinstance(response.body, dict) else {}
        status_value = _normalized_status(body)
        message = _extract_message(body)
        provider_reference = _extract_first_text(
            body,
            (
                "provider_reference",
                "providerReference",
                "funding_reference",
                "fundingReference",
                "transaction_reference",
                "transactionReference",
                "id",
            ),
        ) or fallback_reference

        if status_value in _SUCCESS_STATUSES:
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if status_value in _FAILED_STATUSES:
            return CollectionResult(
                outcome=CollectionOutcome.FAILED_DEFINITE,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if status_value in _PENDING_STATUSES:
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if not status_value:
            fallback_status = (
                f"http_{response.status_code}" if response.status_code else "transport_error"
            )
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=fallback_status,
                message=message or malformed_message,
            )

        return CollectionResult(
            outcome=CollectionOutcome.UNKNOWN,
            provider_reference=provider_reference,
            raw_status=status_value,
            message=message,
        )

    def _parse_payout_result(
        self,
        *,
        response: EconfirmHttpResponse,
        fallback_reference: str,
        malformed_message: str,
    ) -> PayoutResult:
        body = response.body if isinstance(response.body, dict) else {}
        status_value = _normalized_status(body)
        message = _extract_message(body)
        provider_reference = _extract_first_text(
            body,
            (
                "provider_reference",
                "providerReference",
                "payout_reference",
                "payoutReference",
                "transaction_reference",
                "transactionReference",
                "id",
            ),
        ) or fallback_reference

        if status_value in _SUCCESS_STATUSES:
            return PayoutResult(
                outcome=PayoutOutcome.SUCCEEDED,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if status_value in _FAILED_STATUSES:
            return PayoutResult(
                outcome=PayoutOutcome.FAILED_DEFINITE,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if status_value in _PENDING_STATUSES:
            return PayoutResult(
                outcome=PayoutOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=status_value,
                message=message,
            )

        if not status_value:
            fallback_status = (
                f"http_{response.status_code}" if response.status_code else "transport_error"
            )
            return PayoutResult(
                outcome=PayoutOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status=fallback_status,
                message=message or malformed_message,
            )

        return PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            provider_reference=provider_reference,
            raw_status=status_value,
            message=message,
        )

    @staticmethod
    def _build_reference(kind: str, seed: str) -> str:
        suffix = uuid5(NAMESPACE_URL, f"{kind}:{seed}").hex[:20]
        return f"{ECONFIRM_PROVIDER_NAME}-{kind}-{suffix}"


def _normalized_status(payload: dict[str, Any]) -> str:
    candidates = (
        "status",
        "status_code",
        "statusCode",
        "result",
        "state",
        "outcome",
        "code",
    )
    return _extract_first_text(payload, candidates).lower()


def _extract_message(payload: dict[str, Any]) -> str | None:
    message = _extract_first_text(
        payload,
        (
            "message",
            "detail",
            "description",
            "error",
            "status_description",
            "statusDescription",
        ),
    )
    return message or None


def _extract_first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return ""


def _extract_decimal(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Decimal,
) -> Decimal:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return default


def _default_http_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> EconfirmHttpResponse:
    raw_body: bytes | None = None
    if payload is not None:
        raw_body = json.dumps(payload).encode("utf-8")

    request_obj = request.Request(url=url, method=method.upper(), headers=headers, data=raw_body)
    try:
        with request.urlopen(request_obj, timeout=timeout_seconds) as response:
            text_body = response.read().decode("utf-8")
            return EconfirmHttpResponse(
                status_code=response.status,
                body=_safe_json_parse(text_body),
                text=text_body,
            )
    except error.HTTPError as exc:
        text_body = exc.read().decode("utf-8")
        return EconfirmHttpResponse(
            status_code=exc.code,
            body=_safe_json_parse(text_body),
            text=text_body,
        )
    except error.URLError as exc:  # pragma: no cover
        reason = str(exc.reason)
        raise RuntimeError(f"eConfirm transport error for {url}: {reason}") from exc


default_econfirm_http_transport = _default_http_transport


def _safe_json_parse(text_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _redact_payload(value: Any) -> Any:
    sensitive_keys = {
        "phone",
        "phonenumber",
        "msisdn",
        "token",
        "authorization",
        "signature",
        "apikey",
        "apisecret",
        "clientsecret",
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