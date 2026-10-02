import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.services.custody.dto import PayoutRequest
from app.services.custody.enums import PayoutOutcome
from app.services.custody.loop_auth import LoopHttpResponse
from app.services.custody.loop_payout import LoopPayoutRail

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "loop"


class _TokenManagerStub:
    def get_access_token(self) -> str:
        return "loop-access-token"


def _fixture(name: str) -> dict[str, str]:
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _payout_request(purpose: str = "release-712") -> PayoutRequest:
    return PayoutRequest(
        escrow_reference="escrow-712",
        amount=Decimal("150.00"),
        destination_phone="+254712345678",
        purpose=purpose,
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected_outcome", "expected_raw_status"),
    [
        ("payout_request_success.json", PayoutOutcome.SUCCEEDED, "0"),
        ("payout_request_decline.json", PayoutOutcome.FAILED_DEFINITE, "2"),
        ("payout_request_unknown.json", PayoutOutcome.UNKNOWN, "102"),
    ],
)
def test_loop_payout_contract_fixtures_classify_send_money_responses(
    fixture_name: str,
    expected_outcome: PayoutOutcome,
    expected_raw_status: str,
) -> None:
    fixture_body = _fixture(fixture_name)
    calls: list[tuple[str, str, dict[str, str], dict[str, str], float]] = []

    def _transport(method, url, headers, payload, timeout):
        calls.append((method, url, headers, payload, timeout))
        return LoopHttpResponse(status_code=503, body=fixture_body, text=json.dumps(fixture_body))

    rail = LoopPayoutRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=_transport,
        now_fn=lambda: datetime(2026, 10, 2, 13, 20, 0, tzinfo=UTC),
        nonce_fn=lambda: "nonce-contract",
    )

    result = rail.request_payout(_payout_request())

    assert result.outcome == expected_outcome
    assert result.provider_reference == fixture_body["transactionReference"]
    assert result.raw_status == expected_raw_status

    assert len(calls) == 1
    method, url, headers, payload, timeout = calls[0]
    assert method == "POST"
    assert url == "https://sandbox.loop.example/payout/send-money"
    assert headers["Authorization"] == "Bearer loop-access-token"
    assert headers["X-Loop-Timestamp"] == "20261002132000"
    assert headers["X-Loop-Nonce"] == "nonce-contract"
    assert headers["X-Loop-Signature"]
    assert payload["purpose"] == "release-712"
    assert timeout == 10.0


def test_loop_payout_retry_reuses_reference_with_fresh_auth_headers() -> None:
    timestamps = iter(
        [
            datetime(2026, 10, 2, 14, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 2, 14, 0, 10, tzinfo=UTC),
        ]
    )
    nonces = iter(["nonce-retry-a", "nonce-retry-b"])
    headers_seen: list[dict[str, str]] = []
    payloads_seen: list[dict[str, str]] = []

    def _transport(method, url, headers, payload, timeout):
        assert method == "POST"
        assert url == "https://sandbox.loop.example/payout/send-money"
        assert timeout == 10.0
        headers_seen.append(headers)
        payloads_seen.append(payload)
        body = {
            "statusCode": "0",
            "statusDescription": "Accepted",
            "transactionReference": payload["transactionReference"],
        }
        return LoopHttpResponse(status_code=200, body=body, text=json.dumps(body))

    rail = LoopPayoutRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=_transport,
        now_fn=lambda: next(timestamps),
        nonce_fn=lambda: next(nonces),
    )

    request = _payout_request("release-retry")
    first = rail.request_payout(request)
    second = rail.request_payout(request)

    assert first.provider_reference == second.provider_reference
    assert first.outcome == PayoutOutcome.SUCCEEDED
    assert second.outcome == PayoutOutcome.SUCCEEDED

    assert payloads_seen[0]["transactionReference"] == payloads_seen[1]["transactionReference"]
    assert headers_seen[0]["X-Loop-Timestamp"] == "20261002140000"
    assert headers_seen[1]["X-Loop-Timestamp"] == "20261002140010"
    assert headers_seen[0]["X-Loop-Nonce"] == "nonce-retry-a"
    assert headers_seen[1]["X-Loop-Nonce"] == "nonce-retry-b"
    assert headers_seen[0]["X-Loop-Signature"] != headers_seen[1]["X-Loop-Signature"]


def test_loop_payout_duplicate_status_is_treated_as_success() -> None:
    fixture_body = _fixture("payout_request_duplicate.json")

    def _transport(_method, _url, _headers, _payload, _timeout):
        return LoopHttpResponse(status_code=409, body=fixture_body, text=json.dumps(fixture_body))

    rail = LoopPayoutRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=_transport,
        now_fn=lambda: datetime(2026, 10, 2, 14, 20, 0, tzinfo=UTC),
        nonce_fn=lambda: "nonce-dup",
    )

    result = rail.request_payout(_payout_request("release-duplicate"))

    assert result.outcome == PayoutOutcome.SUCCEEDED
    assert result.provider_reference == "loop-payout-fixture-duplicate"
    assert result.raw_status == "duplicate"


@pytest.mark.parametrize(
    ("final_state", "expected_outcome"),
    [
        ("settled", PayoutOutcome.SUCCEEDED),
        ("declined", PayoutOutcome.FAILED_DEFINITE),
        ("pending", PayoutOutcome.UNKNOWN),
    ],
)
def test_loop_payout_inquiry_classifies_terminal_and_pending_states(
    final_state: str,
    expected_outcome: PayoutOutcome,
) -> None:
    def _transport(_method, _url, _headers, _payload, _timeout):
        body = {
            "statusCode": "0",
            "finalState": final_state,
            "transactionReference": "loop-payout-inquiry-712",
        }
        return LoopHttpResponse(status_code=200, body=body, text=json.dumps(body))

    rail = LoopPayoutRail(
        base_url="https://sandbox.loop.example",
        shortcode="600111",
        passkey="loop-passkey",
        token_manager=_TokenManagerStub(),
        transport=_transport,
        now_fn=lambda: datetime(2026, 10, 2, 14, 30, 0, tzinfo=UTC),
        nonce_fn=lambda: "nonce-inquiry",
    )

    result = rail.get_payout_status("loop-payout-inquiry-712")

    assert result.provider_reference == "loop-payout-inquiry-712"
    assert result.outcome == expected_outcome
