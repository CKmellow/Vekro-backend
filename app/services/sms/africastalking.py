from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib import error, parse, request

from app.core.phone import normalize_phone

AFRICASTALKING_LIVE_URL = "https://api.africastalking.com/version1/messaging"
AFRICASTALKING_SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
SANDBOX_USERNAME = "sandbox"

# Africa's Talking per-recipient statusCode values.
_ACCEPTED_STATUS_CODES = frozenset({100, 101, 102})
_TRANSIENT_STATUS_CODES = frozenset({500, 501, 502})
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0)


class SmsOutcome(StrEnum):
    SENT = "sent"
    FAILED_DEFINITE = "failed_definite"
    FAILED_TRANSIENT = "failed_transient"


@dataclass(frozen=True)
class SmsRecipientResult:
    phone: str
    outcome: SmsOutcome
    attempts: int
    status_code: int | None = None
    status: str | None = None
    message_id: str | None = None
    cost: str | None = None


@dataclass(frozen=True)
class SmsSendResult:
    recipients: list[SmsRecipientResult] = field(default_factory=list)
    attempts: int = 0

    @property
    def all_sent(self) -> bool:
        return bool(self.recipients) and all(
            item.outcome == SmsOutcome.SENT for item in self.recipients
        )

    def for_phone(self, phone: str) -> SmsRecipientResult | None:
        for item in self.recipients:
            if item.phone == phone:
                return item
        return None


@dataclass(frozen=True)
class AfricasTalkingHttpResponse:
    status_code: int
    body: dict[str, Any]
    text: str


AfricasTalkingHttpTransport = Callable[
    [str, dict[str, str], dict[str, str], float],
    AfricasTalkingHttpResponse,
]


class AfricasTalkingSettings(Protocol):
    @property
    def africastalking_username(self) -> str: ...

    @property
    def africastalking_api_key(self) -> str: ...

    @property
    def africastalking_sender_id(self) -> str: ...


@dataclass
class _PendingRecipient:
    phone: str
    attempts: int = 0
    last: SmsRecipientResult | None = None


class AfricasTalkingSmsSender:
    def __init__(
        self,
        *,
        username: str,
        api_key: str,
        sender_id: str | None = None,
        base_url: str | None = None,
        transport: AfricasTalkingHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: Sequence[float] = DEFAULT_BACKOFF_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._username = username.strip()
        self._api_key = api_key.strip()
        if not self._username or not self._api_key:
            raise ValueError(
                "AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY must be set to send SMS."
            )
        self._sender_id = (sender_id or "").strip() or None
        self._url = base_url or (
            AFRICASTALKING_SANDBOX_URL
            if self._username.lower() == SANDBOX_USERNAME
            else AFRICASTALKING_LIVE_URL
        )
        self._transport = transport or default_africastalking_http_transport
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._backoff_seconds = tuple(backoff_seconds)
        self._sleep = sleep_fn
        self._logger = logger or logging.getLogger("app.sms.africastalking")

    @classmethod
    def from_settings(
        cls,
        settings: AfricasTalkingSettings,
        **overrides: Any,
    ) -> AfricasTalkingSmsSender:
        return cls(
            username=settings.africastalking_username,
            api_key=settings.africastalking_api_key,
            sender_id=settings.africastalking_sender_id,
            **overrides,
        )

    def send(self, message: str, recipients: Sequence[str]) -> SmsSendResult:
        text = message.strip()
        if not text:
            raise ValueError("SMS message must not be blank.")

        results: dict[str, SmsRecipientResult] = {}
        order: list[str] = []
        pending: dict[str, _PendingRecipient] = {}

        for raw in recipients:
            try:
                phone = normalize_phone(raw)
            except ValueError:
                key = str(raw)
                if key not in results:
                    order.append(key)
                    results[key] = SmsRecipientResult(
                        phone=key,
                        outcome=SmsOutcome.FAILED_DEFINITE,
                        attempts=0,
                        status="InvalidPhoneNumber",
                    )
                continue
            if phone in pending or phone in results:
                continue
            order.append(phone)
            pending[phone] = _PendingRecipient(phone=phone)

        attempt = 0
        while pending and attempt < self._max_attempts:
            if attempt > 0:
                self._sleep(self._backoff_for(attempt))
            attempt += 1

            batch = list(pending.values())
            for item in batch:
                item.attempts += 1
            outcomes = self._send_batch(text, batch)

            for item in batch:
                outcome = outcomes[item.phone]
                item.last = outcome
                if outcome.outcome != SmsOutcome.FAILED_TRANSIENT:
                    results[item.phone] = outcome
                    del pending[item.phone]

        for item in pending.values():
            results[item.phone] = item.last or SmsRecipientResult(
                phone=item.phone,
                outcome=SmsOutcome.FAILED_TRANSIENT,
                attempts=item.attempts,
            )

        ordered = [results[key] for key in order]
        self._logger.info(
            "africastalking_sms_send attempts=%s sent=%s failed_definite=%s failed_transient=%s",
            attempt,
            sum(1 for r in ordered if r.outcome == SmsOutcome.SENT),
            sum(1 for r in ordered if r.outcome == SmsOutcome.FAILED_DEFINITE),
            sum(1 for r in ordered if r.outcome == SmsOutcome.FAILED_TRANSIENT),
        )
        return SmsSendResult(recipients=ordered, attempts=attempt)

    def _backoff_for(self, completed_attempts: int) -> float:
        if not self._backoff_seconds:
            return 0.0
        index = min(completed_attempts - 1, len(self._backoff_seconds) - 1)
        return self._backoff_seconds[index]

    def _send_batch(
        self,
        message: str,
        batch: list[_PendingRecipient],
    ) -> dict[str, SmsRecipientResult]:
        form = {
            "username": self._username,
            "to": ",".join(item.phone for item in batch),
            "message": message,
        }
        if self._sender_id:
            form["from"] = self._sender_id

        headers = {
            "apiKey": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = self._transport(self._url, headers, form, self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            self._logger.warning("africastalking_transport_error error_type=%s", type(exc).__name__)
            return self._uniform(batch, SmsOutcome.FAILED_TRANSIENT, status="TransportError")

        if response.status_code >= 400 or response.status_code < 200:
            outcome = (
                SmsOutcome.FAILED_TRANSIENT
                if response.status_code in _TRANSIENT_HTTP_STATUSES or response.status_code >= 500
                else SmsOutcome.FAILED_DEFINITE
            )
            self._logger.warning(
                "africastalking_http_error status=%s outcome=%s",
                response.status_code,
                outcome.value,
            )
            return self._uniform(batch, outcome, status=f"HTTP {response.status_code}")

        reported = _extract_recipients(response.body)
        outcomes: dict[str, SmsRecipientResult] = {}
        for item in batch:
            entry = reported.get(item.phone)
            if entry is None:
                # Not echoed back: delivery is unconfirmed, so it is retried.
                outcomes[item.phone] = SmsRecipientResult(
                    phone=item.phone,
                    outcome=SmsOutcome.FAILED_TRANSIENT,
                    attempts=item.attempts,
                    status="MissingRecipientStatus",
                )
                continue
            outcomes[item.phone] = _recipient_result(item, entry)
        return outcomes

    @staticmethod
    def _uniform(
        batch: list[_PendingRecipient],
        outcome: SmsOutcome,
        *,
        status: str,
    ) -> dict[str, SmsRecipientResult]:
        return {
            item.phone: SmsRecipientResult(
                phone=item.phone,
                outcome=outcome,
                attempts=item.attempts,
                status=status,
            )
            for item in batch
        }


def _extract_recipients(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = body.get("SMSMessageData") if isinstance(body, dict) else None
    entries = data.get("Recipients") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}

    reported: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("number") or "").strip()
        if not number:
            continue
        try:
            reported[normalize_phone(number)] = entry
        except ValueError:
            reported[number] = entry
    return reported


def _recipient_result(item: _PendingRecipient, entry: dict[str, Any]) -> SmsRecipientResult:
    try:
        status_code = int(entry.get("statusCode"))
    except (TypeError, ValueError):
        status_code = None

    if status_code in _ACCEPTED_STATUS_CODES:
        outcome = SmsOutcome.SENT
    elif status_code in _TRANSIENT_STATUS_CODES or status_code is None:
        outcome = SmsOutcome.FAILED_TRANSIENT
    else:
        outcome = SmsOutcome.FAILED_DEFINITE

    return SmsRecipientResult(
        phone=item.phone,
        outcome=outcome,
        attempts=item.attempts,
        status_code=status_code,
        status=str(entry.get("status") or "") or None,
        message_id=str(entry.get("messageId") or "") or None,
        cost=str(entry.get("cost") or "") or None,
    )


def _default_http_transport(
    url: str,
    headers: dict[str, str],
    form: dict[str, str],
    timeout_seconds: float,
) -> AfricasTalkingHttpResponse:
    data = parse.urlencode(form).encode("utf-8")
    request_obj = request.Request(url=url, method="POST", headers=headers, data=data)
    try:
        with request.urlopen(request_obj, timeout=timeout_seconds) as response:
            text_body = response.read().decode("utf-8")
            return AfricasTalkingHttpResponse(
                status_code=response.status,
                body=_safe_json_parse(text_body),
                text=text_body,
            )
    except error.HTTPError as exc:
        text_body = exc.read().decode("utf-8", errors="replace")
        return AfricasTalkingHttpResponse(
            status_code=exc.code,
            body=_safe_json_parse(text_body),
            text=text_body,
        )


default_africastalking_http_transport = _default_http_transport


def _safe_json_parse(text_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
