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

# Exact eConfirm v1 status strings (case- and space-sensitive, A7). Observed in
# E1 and documented in docs/econfirm-api-observed.md.
STATUS_PENDING = "pending"
STATUS_STK_INITIATED = "stk_initiated"
STATUS_FUNDED = "Escrow Funded"
STATUS_PAYOUT_INITIATED = "payout_initiated"
STATUS_COMPLETED = "Completed"
STATUS_PAYOUT_FAILED = "payout_failed"

# HTTP codes that are definite validation rejections (no retry, no failover).
_DEFINITE_HTTP = frozenset({400, 401, 404, 422})
# HTTP codes that are "in progress / try later" -> UNKNOWN.
_INPROGRESS_HTTP = frozenset({409})


class EconfirmDefiniteError(RuntimeError):
    """A validation/terminal rejection from eConfirm (FAILED_DEFINITE)."""


class EconfirmUnknownError(RuntimeError):
    """An indeterminate outcome (timeout/5xx/Cloudflare/unparseable) -> UNKNOWN.

    For a create (open_escrow) this means the escrow MAY or MAY NOT exist; the
    orchestration layer must mark it needs_reconciliation and must NOT send an
    STK push (A3). No money is at risk because no STK was sent.
    """


@dataclass(frozen=True)
class EconfirmHttpResponse:
    status_code: int
    body: dict[str, Any]
    text: str
    # Set by the real httpx transport (E2). non_json: the response body was not a
    # JSON object (e.g. a Cloudflare HTML page) -> treat as UNKNOWN, never
    # FAILED_DEFINITE. cloudflare_blocked: a Cloudflare WAF rejection (e.g. the
    # Error 1010 User-Agent ban) -> opens the breaker and triggers fallback for new
    # escrows, but is UNKNOWN for an existing eConfirm escrow. Defaults keep older
    # constructions (and the M17 mocked transport) valid.
    non_json: bool = False
    cloudflare_blocked: bool = False


EconfirmHttpTransport = Callable[
    [str, str, dict[str, str], dict[str, Any] | None, float],
    EconfirmHttpResponse,
]


class EconfirmCustodyProvider:
    """eConfirm escrow API v1 adapter (open / fund / status; payout in E4).

    Talks the real v1 protocol observed in E1: Bearer auth, the
    ``{success, message, data}`` envelope, ``POST /transactions`` to open,
    ``POST /payments/stk-push`` to fund, and ``GET /transactions/{id}`` to poll.
    The business taxonomy (SUCCEEDED / FAILED_DEFINITE / UNKNOWN) is derived from
    the exact ``data.status`` strings plus the HTTP code; transport-health
    classification (breaker/fallback) lives in the injected transport (E2).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str = "",
        transport: EconfirmHttpTransport | None = None,
        timeout_seconds: float = 15.0,
        fallback_email: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()  # unused by v1 (Bearer only); kept for wiring
        self._transport = transport or default_econfirm_http_transport
        self._timeout_seconds = timeout_seconds
        self._fallback_email = fallback_email.strip()
        self._logger = logger or logging.getLogger("app.custody.econfirm")

        self._funding_cache: dict[str, CollectionResult] = {}
        self._payout_cache: dict[str, PayoutResult] = {}

    def capabilities(self) -> CustodyCapabilities:
        return CustodyCapabilities(
            holds_funds_structurally=True,
            supports_split_payout=False,
            supports_partial_release=False,
            supports_webhook_auth=False,
        )

    # -- open ------------------------------------------------------------------
    def open_escrow(self, request_model: OpenEscrowRequest) -> EscrowRecord:
        receiver_phone = request_model.receiver_phone
        if not receiver_phone:
            raise EconfirmDefiniteError(
                "eConfirm escrow requires the seller payout phone (receiver_phone)."
            )
        buyer_email = request_model.buyer_email or self._fallback_email
        seller_email = request_model.seller_email or self._fallback_email
        if not buyer_email or not seller_email:
            raise EconfirmDefiniteError(
                "eConfirm escrow requires buyer/seller e-mail or ECONFIRM_FALLBACK_EMAIL."
            )

        description = (request_model.description or f"Vekro escrow {request_model.transaction_id}")[
            :1000
        ]
        if len(description) < 5:
            description = f"{description} (Vekro)"
        terms = (request_model.terms or "Funds released after Vekro confirms delivery.")[:1000]

        payload = {
            "buyer_email": buyer_email,
            "seller_email": seller_email,
            "receiver_phone": _to_msisdn(receiver_phone),
            "amount": int(request_model.amount),
            "currency": request_model.currency,
            "description": description,
            "terms": terms,
        }
        response = self._send(
            "POST", "/transactions", payload, correlation_id=request_model.transaction_id
        )
        data = _data(response.body)
        signal = _http_signal(response)

        if signal == "definite":
            raise EconfirmDefiniteError(
                _message(response.body) or "eConfirm rejected escrow creation."
            )
        escrow_id = _first_text(data, ("id", "transaction_id"))
        if signal == "unknown" or not escrow_id:
            raise EconfirmUnknownError(
                _message(response.body)
                or "eConfirm create returned an indeterminate response; reconcile by reference."
            )

        return EscrowRecord(
            escrow_reference=escrow_id,
            transaction_id=request_model.transaction_id,
            amount=request_model.amount,
            currency=request_model.currency,
            confirmation_code=_first_text(data, ("confirmation_code",)) or None,
            status=_first_text(data, ("status",)) or None,
            max_principal_kes=_decimal_or_none(_integration(data).get("max_principal_kes")),
        )

    # -- fund (collection) -----------------------------------------------------
    def request_funding(self, request_model: FundingRequest) -> CollectionResult:
        cache_key = f"fund:{request_model.escrow_reference}:{request_model.phone_number}"
        cached = self._funding_cache.get(cache_key)
        if cached is not None:
            return cached

        payload = {
            "transaction_id": request_model.escrow_reference,
            "payer_phone": _to_msisdn(request_model.phone_number),
        }
        response = self._send(
            "POST", "/payments/stk-push", payload, correlation_id=request_model.escrow_reference
        )
        result = self._collection_result(
            response, fallback_reference=request_model.escrow_reference
        )
        # Only cache a terminal (non-UNKNOWN) result; an STK prompt is "sent",
        # not "funded", so UNKNOWN must stay pollable (A2/Rule 4).
        if result.outcome != CollectionOutcome.UNKNOWN:
            self._funding_cache[cache_key] = result
        return result

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        response = self._get_transaction(provider_reference)
        return self._collection_result(response, fallback_reference=provider_reference)

    # -- status ----------------------------------------------------------------
    def get_status(self, escrow_reference: str) -> EscrowStatusResult:
        response = self._get_transaction(escrow_reference)
        data = _data(response.body)
        status = _first_text(data, ("status",))
        amount = _decimal_or_none(data.get("amount")) or Decimal("0.00")

        funded = Decimal("0.00")
        funded_states = {
            STATUS_FUNDED,
            STATUS_PAYOUT_INITIATED,
            STATUS_COMPLETED,
            STATUS_PAYOUT_FAILED,
        }
        if status in funded_states:
            funded = amount
        # eConfirm's GET does not split released vs refunded amounts, and
        # payout_initiated/Completed are shared by release and reversal (A4).
        # Those are tracked locally by recorded intent + the ledger (E4/E5), so
        # they stay zero here.
        return EscrowStatusResult(
            escrow_reference=escrow_reference,
            funded_amount=funded,
            released_amount=Decimal("0.00"),
            refunded_amount=Decimal("0.00"),
        )

    # -- payout (endpoints aligned here; full semantics in E4) -----------------
    def request_payout(self, request_model: PayoutRequest) -> PayoutResult:
        kind = "reversal" if _is_reversal(request_model.purpose) else "release"
        return self._submit_payout(request_model, kind=kind)

    def get_payout_status(self, provider_reference: str) -> PayoutResult:
        response = self._get_transaction(provider_reference)
        return self._payout_result(response, fallback_reference=provider_reference)

    def release(self, request_model: PayoutRequest) -> PayoutResult:
        return self._submit_payout(request_model, kind="release")

    def refund(self, request_model: PayoutRequest) -> PayoutResult:
        return self._submit_payout(request_model, kind="reversal")

    def _submit_payout(self, request_model: PayoutRequest, *, kind: str) -> PayoutResult:
        cache_key = f"{kind}:{request_model.escrow_reference}:{request_model.purpose}"
        cached = self._payout_cache.get(cache_key)
        if cached is not None:
            return cached

        notes = request_model.notes or request_model.purpose or "Vekro-authorised payout"
        payload = {
            "confirmation_code": request_model.confirmation_code or "",
            "notes": notes,
        }
        suffix = "reversal" if kind == "reversal" else "release"
        endpoint = f"/transactions/{quote(request_model.escrow_reference, safe='')}/{suffix}"
        response = self._send(
            "POST", endpoint, payload, correlation_id=request_model.escrow_reference
        )
        result = self._payout_result(response, fallback_reference=request_model.escrow_reference)
        if result.outcome != PayoutOutcome.UNKNOWN:
            self._payout_cache[cache_key] = result
        return result

    # -- response mapping ------------------------------------------------------
    def _collection_result(
        self, response: EconfirmHttpResponse, *, fallback_reference: str
    ) -> CollectionResult:
        data = _data(response.body)
        status = _first_text(data, ("status",))
        message = _message(response.body)
        provider_reference = _first_text(data, ("id", "transaction_id")) or fallback_reference
        signal = _http_signal(response)

        if signal == "definite":
            outcome = CollectionOutcome.FAILED_DEFINITE
        elif signal == "ok":
            outcome = (
                CollectionOutcome.SUCCEEDED
                if status == STATUS_FUNDED
                else CollectionOutcome.UNKNOWN
            )
        else:
            outcome = CollectionOutcome.UNKNOWN

        return CollectionResult(
            outcome=outcome,
            provider_reference=provider_reference,
            raw_status=status or _fallback_status(response),
            message=message,
        )

    def _payout_result(
        self, response: EconfirmHttpResponse, *, fallback_reference: str
    ) -> PayoutResult:
        data = _data(response.body)
        status = _first_text(data, ("status",))
        message = _message(response.body)
        provider_reference = _first_text(data, ("id", "transaction_id")) or fallback_reference
        signal = _http_signal(response)

        if signal == "definite":
            outcome = PayoutOutcome.FAILED_DEFINITE
        elif signal == "ok":
            if status == STATUS_COMPLETED:
                outcome = PayoutOutcome.SUCCEEDED
            elif status == STATUS_PAYOUT_FAILED:
                outcome = PayoutOutcome.FAILED_DEFINITE
            else:  # payout_initiated / unexpected -> still in progress
                outcome = PayoutOutcome.UNKNOWN
        else:
            outcome = PayoutOutcome.UNKNOWN

        return PayoutResult(
            outcome=outcome,
            provider_reference=provider_reference,
            raw_status=status or _fallback_status(response),
            message=message,
        )

    # -- transport -------------------------------------------------------------
    def _get_transaction(self, reference: str) -> EconfirmHttpResponse:
        endpoint = f"/transactions/{quote(reference, safe='')}"
        return self._send("GET", endpoint, None, correlation_id=reference)

    def _send(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None,
        *,
        correlation_id: str | None = None,
    ) -> EconfirmHttpResponse:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id

        try:
            return self._transport(method, url, headers, payload, self._timeout_seconds)
        except Exception as exc:  # pragma: no cover - exercised via transport-error tests
            from app.services.custody.redaction import safe_error_message

            self._logger.warning(
                "econfirm_transport_error endpoint=%s error=%s",
                endpoint,
                safe_error_message(exc),
            )
            return EconfirmHttpResponse(
                status_code=0,
                body={"status": "transport_error", "message": safe_error_message(exc)},
                text="",
            )


# --- module helpers -----------------------------------------------------------
def _http_signal(response: EconfirmHttpResponse) -> str:
    """Classify an HTTP response into 'ok' | 'definite' | 'unknown'.

    'ok' means a well-formed 2xx whose business status still needs reading.
    'definite' is a validation rejection (no retry/failover). 'unknown' is any
    indeterminate condition (transport error, Cloudflare, non-JSON, 409, 429, 5xx).
    """
    if response.status_code == 0 or response.cloudflare_blocked or response.non_json:
        return "unknown"
    code = response.status_code
    if 200 <= code < 300:
        return "ok"
    if code in _DEFINITE_HTTP:
        return "definite"
    if code in _INPROGRESS_HTTP or code == 429 or code >= 500:
        return "unknown"
    return "unknown"


def _data(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict):
        return data
    return body


def _integration(data: dict[str, Any]) -> dict[str, Any]:
    integration = data.get("integration")
    return integration if isinstance(integration, dict) else {}


def _message(body: dict[str, Any]) -> str | None:
    if not isinstance(body, dict):
        return None
    message = _first_text(body, ("message", "detail", "error", "title"))
    return message or None


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return ""


def _fallback_status(response: EconfirmHttpResponse) -> str:
    if response.cloudflare_blocked:
        return "cloudflare_blocked"
    if response.non_json:
        return "non_json"
    if response.status_code == 0:
        return "transport_error"
    return f"http_{response.status_code}"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _is_reversal(purpose: str) -> bool:
    lowered = purpose.lower()
    return "refund" in lowered or "revers" in lowered


def _to_msisdn(phone: str) -> str:
    """eConfirm wants 2547XXXXXXXX with no leading +."""
    value = phone.strip()
    return value[1:] if value.startswith("+") else value


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


def _build_reference(kind: str, seed: str) -> str:
    suffix = uuid5(NAMESPACE_URL, f"{kind}:{seed}").hex[:20]
    return f"{ECONFIRM_PROVIDER_NAME}-{kind}-{suffix}"
