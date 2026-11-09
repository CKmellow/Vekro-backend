"""#249: the Pesapal sandbox bodies recorded by scripts/pesapal_probe.py (docs/pesapal-observed.md),
replayed through the adapter."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.services.custody.dto import FundingRequest
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalHttpResponse
from app.services.custody.pesapal_collection import PesapalCollectionRail

OBSERVED = Path(__file__).parent / "fixtures" / "pesapal" / "observed"


def _observed(name: str) -> tuple[int, Any]:
    capture = json.loads((OBSERVED / name).read_text())
    return capture["http_status"], capture["body"]


class _Token:
    def get_access_token(self) -> str:
        return "token"


def _rail(answer: tuple[int, Any], seen: dict[str, Any] | None = None, **kwargs: Any):
    def transport(method, url, headers, payload, timeout_seconds):
        if seen is not None:
            seen.update(method=method, url=url, payload=payload)
        status, body = answer
        return PesapalHttpResponse(status_code=status, body=body, text=json.dumps(body))

    options = {"ipn_id": "ipn-1", **kwargs}
    return PesapalCollectionRail(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        callback_url="https://app.example/pay/return",
        token_manager=_Token(),
        transport=transport,
        **options,
    )


def _with_code(code: int, description: str) -> tuple[int, Any]:
    status, body = _observed("get_transaction_status_unpaid.json")
    return status, {
        **body,
        "status_code": code,
        "payment_status_description": description,
        "error": None,
    }


def test_an_unpaid_order_reads_as_pending_not_failed() -> None:
    result = _rail(_observed("get_transaction_status_unpaid.json")).get_funding_status("t-1")

    assert result.outcome == CollectionOutcome.UNKNOWN
    assert result.raw_status == "payment_details_not_found"
    assert result.metadata["amount"] == "1.0"
    assert result.metadata["currency"] == "KES"


def test_documented_status_codes() -> None:
    expected = {
        1: CollectionOutcome.SUCCEEDED,
        2: CollectionOutcome.FAILED_DEFINITE,
        3: CollectionOutcome.FAILED_DEFINITE,
        0: CollectionOutcome.UNKNOWN,
        7: CollectionOutcome.UNKNOWN,  # a code Vekro does not know never fails a payment
    }
    for code, outcome in expected.items():
        result = _rail(_with_code(code, "Whatever")).get_funding_status("t-1")
        assert result.outcome == outcome, code


def test_submit_order_sends_only_documented_fields_and_parses_the_answer() -> None:
    seen: dict[str, Any] = {}
    rail = _rail(_observed("submit_order.json"), seen)

    result = rail.request_funding(
        FundingRequest(
            escrow_reference="VK-7K3P9Q",
            amount=Decimal("1.00"),
            phone_number="+254700000000",
            account_reference="VK-7K3P9Q",
        )
    )

    assert result.outcome == CollectionOutcome.SUCCEEDED
    assert result.metadata["redirect_url"].startswith("https://cybqa.pesapal.com/")
    assert set(seen["payload"]) == {
        "id",
        "currency",
        "amount",
        "description",
        "callback_url",
        "notification_id",
        "billing_address",
    }
    assert len(seen["payload"]["description"]) <= 100


def test_a_disabled_rail_without_an_ipn_id_logs_nothing(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        _rail(_observed("submit_order.json"), ipn_id="")
    assert "pesapal_ipn_id_missing" not in caplog.text


@pytest.mark.parametrize(
    ("fixture", "card_last4"),
    [
        ("get_transaction_status_completed_frictionless.json", "0010"),
        ("get_transaction_status_completed_3ds_card.json", "1091"),
        ("get_transaction_status_completed_declined_card.json", "1018"),
    ],
)
def test_observed_sandbox_card_payments_read_as_funded(fixture: str, card_last4: str) -> None:
    """2026-11-09: all three published test cards came back Completed (pesapal-observed.md 8)."""
    result = _rail(_observed(fixture)).get_funding_status("t-1")
    assert result.outcome == CollectionOutcome.SUCCEEDED
    assert result.metadata["amount"] == "10.0" and result.metadata["currency"] == "KES"
    assert result.metadata["payment_method"] == "Visa"
    assert result.metadata["confirmation_code"]
    _status, body = _observed(fixture)
    assert body["payment_account"].endswith(card_last4)
    assert set(body["payment_account"][:-4]) == {"X"}
