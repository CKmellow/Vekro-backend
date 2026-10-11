from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.services.sms import (
    AFRICASTALKING_LIVE_URL,
    AFRICASTALKING_SANDBOX_URL,
    AfricasTalkingHttpResponse,
    AfricasTalkingSmsSender,
    SmsOutcome,
)


def _recipient(number: str, status_code: int, status: str) -> dict[str, Any]:
    return {
        "number": number,
        "statusCode": status_code,
        "status": status,
        "cost": "KES 0.8000",
        "messageId": f"ATXid_{number[-4:]}",
    }


def _ok(recipients: list[dict[str, Any]], status_code: int = 201) -> AfricasTalkingHttpResponse:
    return AfricasTalkingHttpResponse(
        status_code=status_code,
        body={"SMSMessageData": {"Message": "Sent", "Recipients": recipients}},
        text="",
    )


class _FakeTransport:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, headers, form, timeout_seconds) -> AfricasTalkingHttpResponse:
        self.calls.append(
            {"url": url, "headers": headers, "form": form, "timeout": timeout_seconds}
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _sender(transport: _FakeTransport, sleeps: list[float], **overrides) -> AfricasTalkingSmsSender:
    params: dict[str, Any] = {
        "username": "sandbox",
        "api_key": "at-secret-api-key",
        "sender_id": "VEKRO",
        "transport": transport,
        "sleep_fn": sleeps.append,
    }
    params.update(overrides)
    return AfricasTalkingSmsSender(**params)


def test_request_shaping_normalizes_and_dedupes_recipients() -> None:
    transport = _FakeTransport([_ok([_recipient("+254712345678", 101, "Success")])])
    sender = _sender(transport, [])

    result = sender.send("Your code is 1234", ["0712345678", "+254712345678", "254712345678"])

    assert result.all_sent is True
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == AFRICASTALKING_SANDBOX_URL
    assert call["headers"]["apiKey"] == "at-secret-api-key"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call["form"] == {
        "username": "sandbox",
        "to": "+254712345678",
        "message": "Your code is 1234",
        "from": "VEKRO",
    }
    sent = result.for_phone("+254712345678")
    assert sent is not None
    assert sent.message_id == "ATXid_5678"
    assert sent.attempts == 1


def test_live_username_uses_live_endpoint_and_omits_blank_sender_id() -> None:
    transport = _FakeTransport([_ok([_recipient("+254712345678", 100, "Processed")])])
    sender = _sender(transport, [], username="vekro-live", sender_id="  ")

    sender.send("hello", ["+254712345678"])

    assert transport.calls[0]["url"] == AFRICASTALKING_LIVE_URL
    assert "from" not in transport.calls[0]["form"]


def test_http_success_is_not_universal_success_per_recipient_status_wins() -> None:
    transport = _FakeTransport(
        [
            _ok(
                [
                    _recipient("+254712345678", 101, "Success"),
                    _recipient("+254722000111", 403, "InvalidPhoneNumber"),
                    _recipient("+254733000222", 406, "UserInBlacklist"),
                ]
            )
        ]
    )
    sleeps: list[float] = []
    sender = _sender(transport, sleeps)

    result = sender.send("hello", ["+254712345678", "+254722000111", "+254733000222"])

    assert result.all_sent is False
    outcomes = {item.phone: item.outcome for item in result.recipients}
    assert outcomes == {
        "+254712345678": SmsOutcome.SENT,
        "+254722000111": SmsOutcome.FAILED_DEFINITE,
        "+254733000222": SmsOutcome.FAILED_DEFINITE,
    }
    assert len(transport.calls) == 1
    assert sleeps == []


def test_transient_recipient_is_retried_alone_with_backoff() -> None:
    transport = _FakeTransport(
        [
            _ok(
                [
                    _recipient("+254712345678", 101, "Success"),
                    _recipient("+254722000111", 500, "InternalServerError"),
                ]
            ),
            _ok([_recipient("+254722000111", 102, "Queued")]),
        ]
    )
    sleeps: list[float] = []
    sender = _sender(transport, sleeps)

    result = sender.send("hello", ["+254712345678", "+254722000111"])

    assert result.all_sent is True
    assert result.attempts == 2
    assert transport.calls[1]["form"]["to"] == "+254722000111"
    assert sleeps == [0.5]
    retried = result.for_phone("+254722000111")
    assert retried is not None
    assert retried.attempts == 2
    assert retried.status_code == 102


def test_transient_http_errors_exhaust_bounded_retries() -> None:
    unavailable = AfricasTalkingHttpResponse(status_code=503, body={}, text="busy")
    transport = _FakeTransport([unavailable, unavailable, unavailable])
    sleeps: list[float] = []
    sender = _sender(transport, sleeps)

    result = sender.send("hello", ["+254712345678"])

    assert len(transport.calls) == 3
    assert sleeps == [0.5, 1.0]
    only = result.recipients[0]
    assert only.outcome == SmsOutcome.FAILED_TRANSIENT
    assert only.attempts == 3
    assert only.status == "HTTP 503"


def test_transport_exception_is_retried_then_succeeds() -> None:
    transport = _FakeTransport(
        [OSError("connection reset"), _ok([_recipient("+254712345678", 101, "Success")])]
    )
    sleeps: list[float] = []
    sender = _sender(transport, sleeps)

    result = sender.send("hello", ["+254712345678"])

    assert result.all_sent is True
    assert sleeps == [0.5]


def test_auth_failure_is_definite_and_not_retried() -> None:
    transport = _FakeTransport(
        [AfricasTalkingHttpResponse(status_code=401, body={}, text="The supplied API key")]
    )
    sleeps: list[float] = []
    sender = _sender(transport, sleeps)

    result = sender.send("hello", ["+254712345678"])

    assert len(transport.calls) == 1
    assert sleeps == []
    assert result.recipients[0].outcome == SmsOutcome.FAILED_DEFINITE


def test_missing_recipient_status_is_retried_not_assumed_sent() -> None:
    transport = _FakeTransport(
        [
            _ok([]),
            _ok([_recipient("+254712345678", 101, "Success")]),
        ]
    )
    sender = _sender(transport, [])

    result = sender.send("hello", ["+254712345678"])

    assert len(transport.calls) == 2
    assert result.all_sent is True


def test_invalid_local_phone_fails_without_calling_provider() -> None:
    transport = _FakeTransport([])
    sender = _sender(transport, [])

    result = sender.send("hello", ["not-a-phone"])

    assert transport.calls == []
    assert result.recipients[0].outcome == SmsOutcome.FAILED_DEFINITE
    assert result.recipients[0].status == "InvalidPhoneNumber"


def test_logs_never_include_api_key_or_phone(caplog: pytest.LogCaptureFixture) -> None:
    transport = _FakeTransport(
        [
            AfricasTalkingHttpResponse(status_code=500, body={}, text="boom"),
            _ok([_recipient("+254712345678", 101, "Success")]),
        ]
    )
    sender = _sender(transport, [])

    with caplog.at_level("INFO", logger="app.sms.africastalking"):
        sender.send("hello", ["+254712345678"])

    assert "at-secret-api-key" not in caplog.text
    assert "254712345678" not in caplog.text
    assert "africastalking_sms_send" in caplog.text


def test_from_settings_requires_credentials() -> None:
    settings = SimpleNamespace(
        africastalking_username="sandbox",
        africastalking_api_key="",
        africastalking_sender_id="",
    )
    with pytest.raises(ValueError, match="AFRICASTALKING_API_KEY"):
        AfricasTalkingSmsSender.from_settings(settings)

    settings.africastalking_api_key = "key"
    sender = AfricasTalkingSmsSender.from_settings(settings, transport=_FakeTransport([]))
    assert isinstance(sender, AfricasTalkingSmsSender)
