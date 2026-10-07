from __future__ import annotations

from decimal import Decimal

from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.econfirm_provider import EconfirmCustodyProvider, EconfirmHttpResponse
from app.services.custody.enums import CollectionOutcome, PayoutOutcome


def test_econfirm_provider_capabilities_signal_no_split_support() -> None:
    provider = EconfirmCustodyProvider(
        base_url="https://sandbox.econfirm.example",
        api_key="api-key",
        api_secret="api-secret",
        transport=lambda *_: EconfirmHttpResponse(status_code=200, body={}, text="{}"),
    )

    capabilities = provider.capabilities()
    assert capabilities.holds_funds_structurally is True
    assert capabilities.supports_split_payout is False
    assert capabilities.supports_partial_release is False


def test_econfirm_request_shapes_use_expected_endpoints_and_headers() -> None:
    observed: list[dict[str, object]] = []

    def fake_transport(method, url, headers, payload, timeout_seconds):
        observed.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout_seconds,
            }
        )

        if url.endswith("/v1/escrows/open"):
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "accepted", "escrow_reference": "ecf-escrow-123"},
                text="ok",
            )
        if url.endswith("/fund"):
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "succeeded", "provider_reference": "ecf-fund-123"},
                text="ok",
            )
        if url.endswith("/releases"):
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "succeeded", "provider_reference": "ecf-release-123"},
                text="ok",
            )
        if url.endswith("/reversals"):
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "succeeded", "provider_reference": "ecf-refund-123"},
                text="ok",
            )

        return EconfirmHttpResponse(status_code=200, body={"status": "pending"}, text="ok")

    provider = EconfirmCustodyProvider(
        base_url="https://sandbox.econfirm.example",
        api_key="api-key",
        api_secret="api-secret",
        transport=fake_transport,
    )

    escrow = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id="txn-123",
            buyer_id="buyer-123",
            seller_id="seller-123",
            amount=Decimal("300.00"),
            currency="KES",
        )
    )
    provider.request_funding(
        FundingRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("300.00"),
            phone_number="0712345678",
            account_reference="order-300",
        )
    )
    provider.release(
        PayoutRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("120.00"),
            destination_phone="0712345678",
            purpose="release-test",
        )
    )
    provider.refund(
        PayoutRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("60.00"),
            destination_phone="0712345678",
            purpose="refund-test",
        )
    )

    assert len(observed) == 4
    assert str(observed[0]["url"]).endswith("/v1/escrows/open")
    assert str(observed[1]["url"]).endswith("/v1/escrows/ecf-escrow-123/fund")
    assert str(observed[2]["url"]).endswith("/v1/escrows/ecf-escrow-123/releases")
    assert str(observed[3]["url"]).endswith("/v1/escrows/ecf-escrow-123/reversals")

    headers = observed[1]["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Econfirm-Api-Key"] == "api-key"
    assert headers["X-Econfirm-Api-Secret"] == "api-secret"


def test_econfirm_response_parsing_classifies_outcomes() -> None:
    def fake_transport(method, url, headers, payload, timeout_seconds):
        _ = method
        _ = url
        _ = headers
        _ = timeout_seconds
        account_reference = str((payload or {}).get("account_reference") or "")
        purpose = str((payload or {}).get("purpose") or "")

        if account_reference == "order-success":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "succeeded", "provider_reference": "fund-success"},
                text="ok",
            )
        if account_reference == "order-timeout":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "pending", "provider_reference": "fund-timeout"},
                text="ok",
            )
        if account_reference == "order-failed":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "failed", "provider_reference": "fund-failed"},
                text="ok",
            )
        if account_reference == "order-transport-error":
            raise RuntimeError("network down")

        if purpose == "release-success":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "succeeded", "provider_reference": "release-success"},
                text="ok",
            )
        if purpose == "release-timeout":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "processing", "provider_reference": "release-timeout"},
                text="ok",
            )
        if purpose == "refund-failed":
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "failed", "provider_reference": "refund-failed"},
                text="ok",
            )

        return EconfirmHttpResponse(
            status_code=200,
            body={"status": "accepted", "escrow_reference": "ecf-escrow-parse"},
            text="ok",
        )

    provider = EconfirmCustodyProvider(
        base_url="https://sandbox.econfirm.example",
        api_key="api-key",
        api_secret="api-secret",
        transport=fake_transport,
    )

    escrow = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id="txn-parse",
            buyer_id="buyer-parse",
            seller_id="seller-parse",
            amount=Decimal("250.00"),
            currency="KES",
        )
    )

    success_funding = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("250.00"),
            phone_number="0712000000",
            account_reference="order-success",
        )
    )
    unknown_funding = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("10.00"),
            phone_number="0712000000",
            account_reference="order-timeout",
        )
    )
    failed_funding = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("10.00"),
            phone_number="0712000000",
            account_reference="order-failed",
        )
    )
    transport_error_funding = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("10.00"),
            phone_number="0712000000",
            account_reference="order-transport-error",
        )
    )
    success_release = provider.release(
        PayoutRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("100.00"),
            destination_phone="0712000000",
            purpose="release-success",
        )
    )
    unknown_release = provider.release(
        PayoutRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("20.00"),
            destination_phone="0712000000",
            purpose="release-timeout",
        )
    )
    failed_refund = provider.refund(
        PayoutRequest(
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("20.00"),
            destination_phone="0712000000",
            purpose="refund-failed",
        )
    )

    assert success_funding.outcome == CollectionOutcome.SUCCEEDED
    assert unknown_funding.outcome == CollectionOutcome.UNKNOWN
    assert failed_funding.outcome == CollectionOutcome.FAILED_DEFINITE
    assert transport_error_funding.outcome == CollectionOutcome.UNKNOWN
    assert transport_error_funding.raw_status == "transport_error"

    assert success_release.outcome == PayoutOutcome.SUCCEEDED
    assert unknown_release.outcome == PayoutOutcome.UNKNOWN
    assert failed_refund.outcome == PayoutOutcome.FAILED_DEFINITE


def test_econfirm_release_idempotent_retry_returns_cached_reference() -> None:
    release_calls = 0

    def fake_transport(method, url, headers, payload, timeout_seconds):
        nonlocal release_calls
        _ = headers
        _ = timeout_seconds
        if method == "POST" and url.endswith("/v1/escrows/open"):
            return EconfirmHttpResponse(
                status_code=200,
                body={"status": "accepted", "escrow_reference": "ecf-escrow-idem"},
                text="ok",
            )
        if method == "POST" and url.endswith("/releases"):
            release_calls += 1
            return EconfirmHttpResponse(
                status_code=200,
                body={
                    "status": "succeeded",
                    "provider_reference": f"release-ref-{release_calls}",
                },
                text="ok",
            )
        return EconfirmHttpResponse(status_code=200, body={"status": "pending"}, text="ok")

    provider = EconfirmCustodyProvider(
        base_url="https://sandbox.econfirm.example",
        api_key="api-key",
        api_secret="api-secret",
        transport=fake_transport,
    )
    escrow = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id="txn-idem",
            buyer_id="buyer-idem",
            seller_id="seller-idem",
            amount=Decimal("150.00"),
            currency="KES",
        )
    )

    payout_request = PayoutRequest(
        escrow_reference=escrow.escrow_reference,
        amount=Decimal("50.00"),
        destination_phone="0712000000",
        purpose="release-idempotent",
    )
    first = provider.release(payout_request)
    second = provider.release(payout_request)

    assert first.outcome == PayoutOutcome.SUCCEEDED
    assert second.outcome == PayoutOutcome.SUCCEEDED
    assert second.provider_reference == first.provider_reference
    assert release_calls == 1