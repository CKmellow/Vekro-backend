from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.services.custody.dto import FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalHttpResponse
from app.services.custody.pesapal_collection import PesapalCollectionRail

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pesapal"


def _fixture(name: str) -> dict[str, str]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


class _TokenManagerStub:
    def get_access_token(self) -> str:
        return "pesapal-access-token"


def _funding_request() -> FundingRequest:
    return FundingRequest(
        escrow_reference="escrow-pesapal-123",
        amount=Decimal("250.00"),
        phone_number="+254712345678",
        account_reference="order-250",
    )


def test_submit_order_returns_tracking_id_and_redirect_url() -> None:
    observed: dict[str, Any] = {}
    fixture_body = _fixture("submit_order_success.json")

    def _transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return PesapalHttpResponse(
            status_code=200,
            body=fixture_body,
            text="ok",
        )

    rail = PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        ipn_id="ipn-123",
        token_manager=_TokenManagerStub(),
        transport=_transport,
    )

    result = rail.request_funding(_funding_request())

    assert result.outcome == CollectionOutcome.SUCCEEDED
    assert result.provider_reference == "pesapal-track-fixture-1001"
    assert result.metadata == {
        "order_tracking_id": "pesapal-track-fixture-1001",
        "redirect_url": "https://cybqa.pesapal.com/checkout/fixture-1001",
    }

    assert observed["method"] == "POST"
    assert str(observed["url"]).endswith("/api/Transactions/SubmitOrderRequest")
    headers = dict(observed["headers"] or {})
    payload = dict(observed["payload"] or {})
    assert headers["Authorization"] == "Bearer pesapal-access-token"
    assert payload["notification_id"] == "ipn-123"
    assert payload["callback_url"] == "https://backend.example/api/webhooks/pesapal/callback"


def test_submit_order_returns_unknown_when_ipn_id_is_missing() -> None:
    def _unexpected_transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        raise AssertionError("Transport should not be called when IPN ID is missing.")

    rail = PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        ipn_id="",
        token_manager=_TokenManagerStub(),
        transport=_unexpected_transport,
    )

    result = rail.request_funding(_funding_request())

    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "missing_ipn_id"
    assert "Run the IPN registration action" in (result.message or "")


def test_submit_order_missing_redirect_fixture_is_unknown() -> None:
    fixture_body = _fixture("submit_order_missing_redirect.json")

    def _transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = payload
        _ = timeout_seconds
        return PesapalHttpResponse(status_code=200, body=fixture_body, text="ok")

    rail = PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        ipn_id="ipn-123",
        token_manager=_TokenManagerStub(),
        transport=_transport,
    )

    result = rail.request_funding(_funding_request())

    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.provider_reference == "pesapal-track-fixture-3003"
    assert result.metadata == {
        "order_tracking_id": "pesapal-track-fixture-3003",
        "redirect_url": None,
    }


@pytest.mark.parametrize(
    ("fixture_name", "expected_outcome", "expected_reference"),
    [
        (
            "get_transaction_status_completed.json",
            CollectionOutcome.SUCCEEDED,
            "pesapal-track-fixture-1001",
        ),
        (
            "get_transaction_status_failed.json",
            CollectionOutcome.FAILED_DEFINITE,
            "pesapal-track-fixture-4004",
        ),
        (
            "get_transaction_status_pending.json",
            CollectionOutcome.UNKNOWN,
            "pesapal-track-fixture-2002",
        ),
    ],
)
def test_status_inquiry_classifies_finality(
    fixture_name: str,
    expected_outcome: CollectionOutcome,
    expected_reference: str,
) -> None:
    observed: dict[str, Any] = {}
    fixture_body = _fixture(fixture_name)

    def _transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return PesapalHttpResponse(status_code=200, body=fixture_body, text="ok")

    rail = PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        ipn_id="ipn-123",
        token_manager=_TokenManagerStub(),
        transport=_transport,
    )

    result = rail.get_funding_status("pesapal-track-9002")

    assert result.outcome == expected_outcome
    assert result.provider_reference == expected_reference
    assert result.raw_status == fixture_body["payment_status_description"].lower()
    assert observed["method"] == "GET"
    assert "orderTrackingId=pesapal-track-9002" in str(observed["url"])