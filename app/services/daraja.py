"""Safaricom Daraja (M-Pesa Express) transport: OAuth, STK push and STK query (issue #224).

The STK *callback* that Daraja posts is unauthenticated, so it is never trusted on its own: the
payment is recorded only after ``stk_query`` confirms it (app/services/daraja_confirmation.py),
the same rule as the Pesapal and LOOP webhooks.

STK query answers:

- HTTP 200 with ``ResultCode`` "0": paid.
- HTTP 200 with a final failure code (``FINAL_FAILURE_CODES``: 1032 cancelled, 1037 timeout,
  2001 wrong PIN, 1 insufficient funds): not paid.
- HTTP 200 with any other ``ResultCode``: still pending (#245). The sandbox answered one such code
  13 seconds after a push, before the payer could act; failing on it would mark a payment the
  buyer may still approve as failed, and nothing re-checks a failed prompt.
- HTTP 500 with ``errorCode`` 500.001.1001 ("The transaction is being processed"): still pending.
- Anything else (timeouts, 5xx, non-JSON): ``DarajaUnavailableError``, try again later.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx2 as httpx

from app.services.payment import PaymentRequest, PaymentResponse

_logger = logging.getLogger("app.payments.daraja")

BASE_URLS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}
PENDING_ERROR_CODE = "500.001.1001"
# ResultCodes that end a prompt for good (kept equal to payment_prompts' failure reasons).
FINAL_FAILURE_CODES = frozenset({1, 1032, 1037, 2001})
EAT = timezone(timedelta(hours=3))
TOKEN_REFRESH_MARGIN_SECONDS = 60


class DarajaError(RuntimeError):
    """Daraja refused the request (definite)."""


class DarajaUnavailableError(DarajaError):
    """Daraja could not be reached or answered unexpectedly; retry later."""


@dataclass(frozen=True)
class StkQueryResult:
    state: Literal["succeeded", "failed", "pending"]
    result_code: int | None
    result_desc: str


class DarajaClient:
    def __init__(
        self,
        *,
        environment: str,
        consumer_key: str,
        consumer_secret: str,
        shortcode: str,
        passkey: str,
        callback_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if environment not in BASE_URLS:
            raise ValueError("DARAJA_ENV must be sandbox or production.")
        self._http = httpx.Client(
            base_url=BASE_URLS[environment], timeout=timeout_seconds, transport=transport
        )
        self._key = consumer_key
        self._secret = consumer_secret
        self.shortcode = shortcode
        self._passkey = passkey
        self.callback_url = callback_url
        self._clock = clock
        self._now = now
        self._token: str | None = None
        self._token_expires = 0.0

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> DarajaClient:
        return cls(
            environment=settings.daraja_env,
            consumer_key=settings.daraja_consumer_key,
            consumer_secret=settings.daraja_consumer_secret,
            shortcode=settings.daraja_shortcode,
            passkey=settings.daraja_passkey,
            callback_url=settings.daraja_callback_url,
            timeout_seconds=settings.daraja_timeout_seconds,
            **kwargs,
        )

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires:
            return self._token
        basic = base64.b64encode(f"{self._key}:{self._secret}".encode()).decode()
        response = self._send(
            "GET",
            "/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        )
        if response.status_code != 200:
            raise DarajaUnavailableError(f"OAuth failed with HTTP {response.status_code}.")
        body = self._json(response)
        token = str(body.get("access_token") or "")
        if not token:
            raise DarajaUnavailableError("OAuth returned no access token.")
        lifetime = int(body.get("expires_in") or 3599)
        self._token = token
        self._token_expires = self._clock() + max(lifetime - TOKEN_REFRESH_MARGIN_SECONDS, 1)
        return token

    def _password(self) -> tuple[str, str]:
        timestamp = self._now().astimezone(EAT).strftime("%Y%m%d%H%M%S")
        raw = f"{self.shortcode}{self._passkey}{timestamp}".encode()
        return base64.b64encode(raw).decode(), timestamp

    def stk_push(
        self, *, phone: str, amount: int, account_reference: str, description: str
    ) -> dict[str, Any]:
        password, timestamp = self._password()
        msisdn = phone.lstrip("+")
        response = self._authorized(
            "/mpesa/stkpush/v1/processrequest",
            {
                "BusinessShortCode": self.shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount,
                "PartyA": msisdn,
                "PartyB": self.shortcode,
                "PhoneNumber": msisdn,
                "CallBackURL": self.callback_url,
                "AccountReference": account_reference[:12],
                "TransactionDesc": description[:13],
            },
        )
        body = self._json(response)
        if response.status_code != 200 or str(body.get("ResponseCode")) != "0":
            message = body.get("errorMessage") or body.get("ResponseDescription") or "refused"
            if response.status_code >= 500:
                raise DarajaUnavailableError(f"STK push failed: {message}")
            raise DarajaError(f"STK push refused: {message}")
        return body

    def stk_query(self, checkout_request_id: str) -> StkQueryResult:
        password, timestamp = self._password()
        response = self._authorized(
            "/mpesa/stkpushquery/v1/query",
            {
                "BusinessShortCode": self.shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id,
            },
        )
        body = self._json(response)
        if response.status_code == 200 and "ResultCode" in body:
            code = int(str(body["ResultCode"]))
            desc = str(body.get("ResultDesc") or "")
            if code == 0:
                return StkQueryResult("succeeded", code, desc)
            if code in FINAL_FAILURE_CODES:
                return StkQueryResult("failed", code, desc)
            _logger.warning("daraja_query_unrecognised_result code=%s desc=%s", code, desc[:120])
            return StkQueryResult("pending", code, desc)
        if str(body.get("errorCode")) == PENDING_ERROR_CODE:
            return StkQueryResult("pending", None, str(body.get("errorMessage") or ""))
        raise DarajaUnavailableError(
            f"STK query answered HTTP {response.status_code} ({body.get('errorCode', 'no code')})."
        )

    def _authorized(self, path: str, body: dict[str, Any]) -> httpx.Response:
        token = self._access_token()
        return self._send("POST", path, json=body, headers={"Authorization": f"Bearer {token}"})

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DarajaUnavailableError(f"{type(exc).__name__} calling Daraja.") from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise DarajaUnavailableError(
                f"Daraja answered HTTP {response.status_code} without JSON."
            ) from exc
        return body if isinstance(body, dict) else {}


def whole_shillings(amount: str | Decimal) -> int:
    """M-Pesa takes whole shillings; Vekro never charges cents."""
    value = Decimal(str(amount))
    if value != value.to_integral_value() or value <= 0:
        raise ValueError(f"M-Pesa amounts must be whole, positive shillings, not {value}.")
    return int(value)


class DarajaStkGateway:
    """The real PaymentGateway behind DARAJA_ENABLED=true."""

    provider = "mpesa_daraja"

    def __init__(self, client: DarajaClient) -> None:
        self._client = client

    def initiate_stk_push(self, request: PaymentRequest) -> PaymentResponse:
        if not request.phone_number:
            raise DarajaError("The buyer has no M-Pesa number.")
        body = self._client.stk_push(
            phone=request.phone_number,
            amount=whole_shillings(request.amount),
            account_reference=request.account_reference,
            description=request.transaction_desc,
        )
        return PaymentResponse(
            provider=self.provider,
            status="queued",
            provider_request_id=str(body.get("MerchantRequestID") or ""),
            merchant_request_id=str(body.get("MerchantRequestID") or ""),
            checkout_request_id=str(body.get("CheckoutRequestID") or ""),
            customer_message=str(body.get("CustomerMessage") or ""),
        )
