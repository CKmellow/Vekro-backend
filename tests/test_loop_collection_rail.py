from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.services.custody.dto import FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.loop_auth import LoopHttpResponse
from app.services.custody.loop_collection import LoopCollectionRail


class _TokenManagerStub:
    def get_access_token(self) -> str:
        return "loop-access-token"


def _funding_request() -> FundingRequest:
    return FundingRequest(
        escrow_reference="escrow-xyz",
        amount=Decimal("150.00"),
        phone_number="+254712345678",
        account_reference="order-150",
    )


def test_prompt_uses_body_status_code_not_http_status() -> None:
    observed: dict[str, object] = {}

    def fake_transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return LoopHttpResponse(
            status_code=503,
            body={
                "statusCode": "0",
                "transactionReference": "loop-ref-123",
                "message": "Accepted by provider",
            },
            text="ignored-http-status",
        )

    rail = LoopCollectionRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=fake_transport,
        now_fn=lambda: datetime(2026, 10, 2, 11, 0, tzinfo=UTC),
        nonce_fn=lambda: "nonce-abc",
    )

    result = rail.request_funding(_funding_request())

    assert result.outcome == CollectionOutcome.SUCCEEDED
    assert result.provider_reference == "loop-ref-123"
    assert result.raw_status == "0"
    assert observed["method"] == "POST"
    assert str(observed["url"]).endswith("/collection/prompt")
    headers = dict(observed["headers"] or {})
    assert headers["Authorization"] == "Bearer loop-access-token"
    assert headers["X-Loop-Timestamp"] == "20261002110000"
    assert headers["X-Loop-Nonce"] == "nonce-abc"
    assert headers["X-Loop-Signature"]


@pytest.mark.parametrize(
    ("final_state", "expected_outcome"),
    [
        ("COMPLETED", CollectionOutcome.SUCCEEDED),
        ("DECLINED", CollectionOutcome.FAILED_DEFINITE),
        ("PENDING", CollectionOutcome.UNKNOWN),
    ],
)
def test_inquiry_distinguishes_terminal_and_pending_states(
    final_state: str,
    expected_outcome: CollectionOutcome,
) -> None:
    def fake_transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return LoopHttpResponse(
            status_code=200,
            body={
                "statusCode": "200",
                "finalState": final_state,
                "transactionReference": "loop-inquiry-123",
            },
            text="ok",
        )

    rail = LoopCollectionRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=fake_transport,
    )

    result = rail.get_funding_status("loop-inquiry-123")
    assert result.outcome == expected_outcome
    assert result.provider_reference == "loop-inquiry-123"
    assert result.raw_status == "200"


def test_malformed_response_is_unknown_and_logs_redacted_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fake_transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return LoopHttpResponse(
            status_code=200,
            body={
                "phoneNumber": "+254712345678",
                "token": "super-secret-token",
                "message": "missing status code",
            },
            text="{}",
        )

    rail = LoopCollectionRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=fake_transport,
    )

    with caplog.at_level("WARNING"):
        result = rail.request_funding(_funding_request())

    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "malformed_response"
    assert "+254712345678" not in caplog.text
    assert "super-secret-token" not in caplog.text
    assert "***" in caplog.text
