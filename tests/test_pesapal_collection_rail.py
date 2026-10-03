from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.custody.dto import FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalHttpResponse
from app.services.custody.pesapal_collection import PesapalCollectionRail


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
    observed: dict[str, object] = {}

    def _transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return PesapalHttpResponse(
            status_code=200,
            body={
                "order_tracking_id": "pesapal-track-9001",
                "redirect_url": "https://cybqa.pesapal.com/checkout/9001",
                "status": "200",
            },
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
    assert result.provider_reference == "pesapal-track-9001"
    assert result.metadata == {
        "order_tracking_id": "pesapal-track-9001",
        "redirect_url": "https://cybqa.pesapal.com/checkout/9001",
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


@pytest.mark.parametrize(
    ("status_value", "expected_outcome"),
    [
        ("COMPLETED", CollectionOutcome.SUCCEEDED),
        ("FAILED", CollectionOutcome.FAILED_DEFINITE),
        ("PENDING", CollectionOutcome.UNKNOWN),
    ],
)
def test_status_inquiry_classifies_finality(
    status_value: str,
    expected_outcome: CollectionOutcome,
) -> None:
    observed: dict[str, object] = {}

    def _transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return PesapalHttpResponse(
            status_code=200,
            body={
                "orderTrackingId": "pesapal-track-9002",
                "payment_status_description": status_value,
                "description": "Status update",
            },
            text="ok",
        )

    rail = PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        ipn_id="ipn-123",
        token_manager=_TokenManagerStub(),
        transport=_transport,
    )

    result = rail.get_funding_status("pesapal-track-9002")

    assert result.outcome == expected_outcome
    assert result.provider_reference == "pesapal-track-9002"
    assert result.raw_status == status_value.lower()
    assert observed["method"] == "GET"
    assert "orderTrackingId=pesapal-track-9002" in str(observed["url"])