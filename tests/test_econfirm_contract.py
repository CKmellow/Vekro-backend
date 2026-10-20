"""E6: contract tests replaying the sanitised E1 captures through the real transport.

Each test feeds a captured eConfirm response (``tests/fixtures/econfirm``) to the
production adapter + httpx transport and asserts both sides of the contract:
the request Vekro sends matches what was observed, and the captured response
maps to the expected Vekro DTO.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome

from tests import econfirm_mock

CREATE = econfirm_mock.load_fixture("create_transaction")
GET = econfirm_mock.load_fixture("get_transaction")


def _open_request() -> OpenEscrowRequest:
    return OpenEscrowRequest(
        transaction_id="VK-CONTRACT",
        buyer_id="buyer",
        seller_id="seller",
        amount=Decimal("100.00"),
        currency="KES",
        buyer_email="buyer@example.com",
        seller_email="seller@example.com",
        receiver_phone="+254712345678",
    )


def test_create_request_matches_observed_contract_and_maps_capture():
    seen = []

    def handler(request):
        seen.append(request)
        return econfirm_mock.fixture_response("create_transaction")

    record = econfirm_mock.build_provider(handler).open_escrow(_open_request())

    request = seen[0]
    assert request.method == CREATE["request"]["method"]
    assert str(request.url) == CREATE["request"]["url"]
    assert request.headers["Authorization"] == "Bearer ek_test_key"
    body = econfirm_mock.request_json(request)
    assert set(body) == {
        "buyer_email",
        "seller_email",
        "receiver_phone",
        "amount",
        "currency",
        "description",
        "terms",
    }
    assert body["receiver_phone"] == "254712345678"  # MSISDN, no leading +
    assert body["amount"] == 100 and isinstance(body["amount"], int)
    assert len(body["description"]) >= 5

    data = CREATE["response"]["json"]["data"]
    assert record.escrow_reference == data["id"] == "txn_SAMPLE0001"
    assert record.confirmation_code == data["confirmation_code"]
    assert record.status == "pending"
    assert record.max_principal_kes == Decimal("500000")


def test_get_capture_maps_to_pending_unfunded_status():
    seen = []

    def handler(request):
        seen.append(request)
        return econfirm_mock.fixture_response("get_transaction")

    provider = econfirm_mock.build_provider(handler)
    status = provider.get_status("txn_SAMPLE0001")
    funding = provider.get_funding_status("txn_SAMPLE0001")

    assert str(seen[0].url) == GET["request"]["url"]
    assert seen[0].method == "GET"
    assert status.escrow_reference == "txn_SAMPLE0001"
    assert status.funded_amount == Decimal("0.00")
    # "pending" is not funded: the poller keeps waiting (Hard Rule 4).
    assert funding.outcome == CollectionOutcome.UNKNOWN
    assert funding.raw_status == "pending"


@pytest.mark.parametrize(
    ("status", "funded", "funding_outcome", "payout_outcome"),
    [
        ("pending", Decimal("0.00"), CollectionOutcome.UNKNOWN, PayoutOutcome.UNKNOWN),
        ("stk_initiated", Decimal("0.00"), CollectionOutcome.UNKNOWN, PayoutOutcome.UNKNOWN),
        ("Escrow Funded", Decimal("100"), CollectionOutcome.SUCCEEDED, PayoutOutcome.UNKNOWN),
        ("payout_initiated", Decimal("100"), CollectionOutcome.UNKNOWN, PayoutOutcome.UNKNOWN),
        ("Completed", Decimal("100"), CollectionOutcome.UNKNOWN, PayoutOutcome.SUCCEEDED),
        ("payout_failed", Decimal("100"), CollectionOutcome.UNKNOWN, PayoutOutcome.FAILED_DEFINITE),
    ],
)
def test_get_capture_status_lifecycle_mapping(status, funded, funding_outcome, payout_outcome):
    def handler(_request):
        return econfirm_mock.fixture_response("get_transaction", status=status)

    provider = econfirm_mock.build_provider(handler)

    assert provider.get_status("txn_SAMPLE0001").funded_amount == funded
    assert provider.get_funding_status("txn_SAMPLE0001").outcome == funding_outcome
    assert provider.get_payout_status("txn_SAMPLE0001").outcome == payout_outcome


def test_stk_push_request_shape():
    seen = []

    def handler(request):
        seen.append(request)
        return econfirm_mock.envelope(id="txn_SAMPLE0001", status="stk_initiated")

    result = econfirm_mock.build_provider(handler).request_funding(
        FundingRequest(
            escrow_reference="txn_SAMPLE0001",
            amount=Decimal("100.00"),
            phone_number="0712345678",
            account_reference="VK-CONTRACT",
        )
    )

    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{econfirm_mock.BASE_URL}/payments/stk-push"
    assert econfirm_mock.request_json(seen[0]) == {
        "transaction_id": "txn_SAMPLE0001",
        "payer_phone": "254712345678",
    }
    assert result.outcome == CollectionOutcome.UNKNOWN  # a prompt is not funding


@pytest.mark.parametrize(
    ("method_name", "suffix"), [("release", "release"), ("refund", "reversal")]
)
def test_payout_request_shape(method_name, suffix):
    seen = []

    def handler(request):
        seen.append(request)
        return econfirm_mock.envelope(id="txn_SAMPLE0001", status="payout_initiated")

    provider = econfirm_mock.build_provider(handler)
    getattr(provider, method_name)(
        PayoutRequest(
            escrow_reference="txn_SAMPLE0001",
            amount=Decimal("100.00"),
            destination_phone="+254712345678",
            purpose=f"tx-{suffix}:VK-CONTRACT",
            confirmation_code="REDACTED_CODE",
        )
    )

    assert str(seen[0].url) == f"{econfirm_mock.BASE_URL}/transactions/txn_SAMPLE0001/{suffix}"
    body = econfirm_mock.request_json(seen[0])
    assert body["confirmation_code"] == "REDACTED_CODE"
    assert body["notes"].strip()
