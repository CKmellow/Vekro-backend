"""E3: eConfirm adapter aligned to the real v1 API (open / fund / status).

Payout *semantics* (confirmation-code persistence, GET-before-retry, ledger
posting) land in E4; these tests cover the adapter's protocol: endpoints, Bearer
auth, the {success, message, data} envelope, and exact-string status mapping.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.custody.dto import (
    FundingRequest,
    OpenEscrowRequest,
    PayoutRequest,
)
from app.services.custody.econfirm_provider import (
    EconfirmCustodyProvider,
    EconfirmDefiniteError,
    EconfirmHttpResponse,
    EconfirmUnknownError,
)
from app.services.custody.enums import CollectionOutcome, PayoutOutcome

BASE = "https://api.econfirm.co.ke/api/v1"


def _envelope(data: dict, *, success: bool = True, message: str = "ok") -> dict:
    return {"success": success, "message": message, "data": data}


def _provider(transport, *, fallback_email: str = "ops@vekro.example") -> EconfirmCustodyProvider:
    return EconfirmCustodyProvider(
        base_url=BASE,
        api_key="ek_testkey",
        transport=transport,
        fallback_email=fallback_email,
    )


def _open_request(**overrides) -> OpenEscrowRequest:
    base = dict(
        transaction_id="VK-ABC123",
        buyer_id="buyer-uuid",
        seller_id="seller-uuid",
        amount=Decimal("500.00"),
        currency="KES",
        receiver_phone="+254712345678",
    )
    base.update(overrides)
    return OpenEscrowRequest(**base)


# --- capabilities -------------------------------------------------------------
def test_capabilities_no_split():
    provider = _provider(lambda *_: EconfirmHttpResponse(200, {}, "{}"))
    caps = provider.capabilities()
    assert caps.holds_funds_structurally is True
    assert caps.supports_split_payout is False
    assert caps.supports_partial_release is False


# --- open ---------------------------------------------------------------------
def test_open_escrow_hits_transactions_with_bearer_and_real_body():
    seen = {}

    def transport(method, url, headers, payload, timeout):
        seen.update(method=method, url=url, headers=headers, payload=payload)
        return EconfirmHttpResponse(
            201,
            _envelope(
                {
                    "id": "txn_123ABC",
                    "status": "pending",
                    "confirmation_code": "AB12CD34",
                    "integration": {
                        "api_account_email_verified": True,
                        "max_principal_kes": 500000,
                    },
                },
                message="Escrow transaction created successfully",
            ),
            "{}",
        )

    record = _provider(transport).open_escrow(_open_request())

    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/transactions"
    assert seen["headers"]["Authorization"] == "Bearer ek_testkey"
    assert seen["headers"]["X-Correlation-Id"] == "VK-ABC123"
    assert seen["payload"]["receiver_phone"] == "254712345678"  # msisdn, no plus
    assert seen["payload"]["amount"] == 500  # whole shillings, int
    assert seen["payload"]["buyer_email"] == "ops@vekro.example"  # fallback used
    assert len(seen["payload"]["description"]) >= 5

    assert record.escrow_reference == "txn_123ABC"
    assert record.confirmation_code == "AB12CD34"
    assert record.status == "pending"
    assert record.max_principal_kes == Decimal("500000.00")


def test_open_escrow_requires_receiver_phone():
    provider = _provider(lambda *_: EconfirmHttpResponse(201, _envelope({"id": "x"}), "{}"))
    with pytest.raises(EconfirmDefiniteError):
        provider.open_escrow(_open_request(receiver_phone=None))


def test_open_escrow_requires_an_email_or_fallback():
    provider = _provider(
        lambda *_: EconfirmHttpResponse(201, _envelope({"id": "x"}), "{}"),
        fallback_email="",
    )
    with pytest.raises(EconfirmDefiniteError):
        provider.open_escrow(_open_request())


def test_open_escrow_422_is_definite():
    def transport(*_):
        return EconfirmHttpResponse(
            422, _envelope({}, success=False, message="amount too low"), "{}"
        )

    with pytest.raises(EconfirmDefiniteError):
        _provider(transport).open_escrow(_open_request())


def test_open_escrow_5xx_is_unknown_for_reconciliation():
    def transport(*_):
        return EconfirmHttpResponse(502, {"message": "bad gateway"}, "{}")

    with pytest.raises(EconfirmUnknownError):
        _provider(transport).open_escrow(_open_request())


def test_open_escrow_missing_id_is_unknown():
    def transport(*_):
        return EconfirmHttpResponse(201, _envelope({"status": "pending"}), "{}")  # no id

    with pytest.raises(EconfirmUnknownError):
        _provider(transport).open_escrow(_open_request())


# --- fund (stk-push) ----------------------------------------------------------
def test_request_funding_prompt_sent_is_unknown_not_funded():
    seen = {}

    def transport(method, url, headers, payload, timeout):
        seen.update(url=url, payload=payload)
        return EconfirmHttpResponse(
            200, _envelope({"id": "txn_1", "status": "stk_initiated"}), "{}"
        )

    result = _provider(transport).request_funding(
        FundingRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            phone_number="0712345678",
            account_reference="VK-ABC123",
        )
    )
    assert seen["url"] == f"{BASE}/payments/stk-push"
    assert seen["payload"] == {"transaction_id": "txn_1", "payer_phone": "254712345678"}
    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "stk_initiated"


def test_request_funding_422_is_definite():
    def transport(*_):
        return EconfirmHttpResponse(422, _envelope({}, success=False, message="over cap"), "{}")

    result = _provider(transport).request_funding(
        FundingRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            phone_number="0712345678",
            account_reference="VK-1",
        )
    )
    assert result.outcome == CollectionOutcome.FAILED_DEFINITE


def test_get_funding_status_funded_is_succeeded():
    def transport(method, url, headers, payload, timeout):
        assert method == "GET" and url == f"{BASE}/transactions/txn_1"
        return EconfirmHttpResponse(
            200, _envelope({"id": "txn_1", "status": "Escrow Funded"}), "{}"
        )

    result = _provider(transport).get_funding_status("txn_1")
    assert result.outcome == CollectionOutcome.SUCCEEDED
    assert result.raw_status == "Escrow Funded"


def test_transport_error_is_unknown():
    def transport(*_):
        raise RuntimeError("network down")

    result = _provider(transport).get_funding_status("txn_1")
    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "transport_error"


def test_cloudflare_block_is_unknown_not_definite():
    def transport(*_):
        return EconfirmHttpResponse(403, {}, "blocked", cloudflare_blocked=True)

    result = _provider(transport).get_funding_status("txn_1")
    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "cloudflare_blocked"


def test_non_json_is_unknown_not_definite():
    def transport(*_):
        return EconfirmHttpResponse(200, {}, "<html>nope</html>", non_json=True)

    result = _provider(transport).get_funding_status("txn_1")
    assert result.outcome == CollectionOutcome.UNKNOWN


# --- status -------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected_funded",
    [
        ("pending", Decimal("0.00")),
        ("stk_initiated", Decimal("0.00")),
        ("Escrow Funded", Decimal("500.00")),
        ("payout_initiated", Decimal("500.00")),
        ("Completed", Decimal("500.00")),
    ],
)
def test_get_status_maps_funded_amount(status, expected_funded):
    def transport(*_):
        return EconfirmHttpResponse(
            200, _envelope({"id": "txn_1", "status": status, "amount": 500}), "{}"
        )

    status_result = _provider(transport).get_status("txn_1")
    assert status_result.funded_amount == expected_funded
    assert status_result.released_amount == Decimal("0.00")
    assert status_result.refunded_amount == Decimal("0.00")


# --- payout endpoint alignment (full semantics in E4) -------------------------
def test_release_posts_to_release_endpoint_with_code_and_notes():
    seen = {}

    def transport(method, url, headers, payload, timeout):
        seen.update(url=url, payload=payload)
        return EconfirmHttpResponse(
            200, _envelope({"id": "txn_1", "status": "payout_initiated"}), "{}"
        )

    result = _provider(transport).release(
        PayoutRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            destination_phone="+254712345678",
            purpose="release",
            confirmation_code="AB12CD34",
            notes="Delivery confirmed",
        )
    )
    assert seen["url"] == f"{BASE}/transactions/txn_1/release"
    assert seen["payload"] == {"confirmation_code": "AB12CD34", "notes": "Delivery confirmed"}
    assert result.outcome == PayoutOutcome.UNKNOWN  # payout_initiated = in progress


def test_release_completed_is_succeeded():
    def transport(*_):
        return EconfirmHttpResponse(200, _envelope({"id": "txn_1", "status": "Completed"}), "{}")

    result = _provider(transport).release(
        PayoutRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            destination_phone="+254712345678",
            purpose="release",
            confirmation_code="AB12CD34",
        )
    )
    assert result.outcome == PayoutOutcome.SUCCEEDED


def test_release_payout_failed_is_definite():
    def transport(*_):
        return EconfirmHttpResponse(
            200, _envelope({"id": "txn_1", "status": "payout_failed"}), "{}"
        )

    result = _provider(transport).release(
        PayoutRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            destination_phone="+254712345678",
            purpose="release",
            confirmation_code="AB12CD34",
        )
    )
    assert result.outcome == PayoutOutcome.FAILED_DEFINITE


def test_reversal_defaults_nonempty_notes_and_hits_reversal_endpoint():
    seen = {}

    def transport(method, url, headers, payload, timeout):
        seen.update(url=url, payload=payload)
        return EconfirmHttpResponse(200, _envelope({"id": "txn_1", "status": "Completed"}), "{}")

    result = _provider(transport).refund(
        PayoutRequest(
            escrow_reference="txn_1",
            amount=Decimal("500.00"),
            destination_phone="+254712345678",
            purpose="buyer-refund",
            confirmation_code="AB12CD34",
        )
    )
    assert seen["url"] == f"{BASE}/transactions/txn_1/reversal"
    assert seen["payload"]["notes"]  # non-empty (A5)
    assert result.outcome == PayoutOutcome.SUCCEEDED


def test_release_409_in_progress_is_unknown_and_not_cached():
    calls = {"n": 0}

    def transport(method, url, headers, payload, timeout):
        calls["n"] += 1
        return EconfirmHttpResponse(409, _envelope({}, success=False, message="in progress"), "{}")

    provider = _provider(transport)
    req = PayoutRequest(
        escrow_reference="txn_1",
        amount=Decimal("500.00"),
        destination_phone="+254712345678",
        purpose="release",
        confirmation_code="AB12CD34",
    )
    first = provider.release(req)
    provider.release(req)
    assert first.outcome == PayoutOutcome.UNKNOWN
    assert calls["n"] == 2  # UNKNOWN not cached -> both hit the API


def test_release_succeeded_is_cached_idempotently():
    calls = {"n": 0}

    def transport(method, url, headers, payload, timeout):
        calls["n"] += 1
        return EconfirmHttpResponse(
            200, _envelope({"id": f"txn_{calls['n']}", "status": "Completed"}), "{}"
        )

    provider = _provider(transport)
    req = PayoutRequest(
        escrow_reference="txn_1",
        amount=Decimal("500.00"),
        destination_phone="+254712345678",
        purpose="release",
        confirmation_code="AB12CD34",
    )
    first = provider.release(req)
    second = provider.release(req)
    assert first.outcome == second.outcome == PayoutOutcome.SUCCEEDED
    assert first.provider_reference == second.provider_reference
    assert calls["n"] == 1  # second call served from cache
