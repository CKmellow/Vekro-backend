"""E6: opt-in live smoke tests against the real eConfirm API.

Skipped unless ``RUN_ECONFIRM_LIVE_TESTS=1``. Run with::

    RUN_ECONFIRM_LIVE_TESTS=1 .venv/bin/pytest -m live -k econfirm -s

``test_econfirm_live_ping`` moves no money. The KES 100 round trip moves real
money and additionally requires ``ECONFIRM_LIVE_MONEY_CONFIRM=KES100`` plus the
two phone numbers below. It runs once: open a KES 100 escrow, send the STK
prompt (the payer must approve it on their phone), wait for "Escrow Funded",
then reverse it back to the payer and wait for "Completed". Escrow ids are
printed so an interrupted run can be reconciled in the eConfirm dashboard.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from app.core.settings import get_settings
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.registry import build_econfirm_provider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_ECONFIRM_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
        reason="Opt-in live test. Set RUN_ECONFIRM_LIVE_TESTS=1 to enable.",
    ),
]

LIVE_AMOUNT = Decimal("100.00")
POLL_SECONDS = 10
FUNDING_WAIT_SECONDS = 180
PAYOUT_WAIT_SECONDS = 300


def _settings():
    settings = get_settings()
    if not settings.econfirm_api_key.strip():
        pytest.skip("Set ECONFIRM_API_KEY for the live eConfirm smoke test.")
    return settings


def _poll(check, *, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    result = check()
    while not result[0] and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        result = check()
    return result


def test_econfirm_live_ping() -> None:
    from app.services.custody.econfirm_transport import build_econfirm_transport

    settings = _settings()
    base = urlsplit(settings.econfirm_base_url)
    # /ping lives at /api/ping, one level above /api/v1 (observed in E1).
    ping_url = f"{base.scheme}://{base.netloc}/api/ping"
    response = build_econfirm_transport(settings)("GET", ping_url, {}, None, 15.0)

    assert response.cloudflare_blocked is False, "Cloudflare blocked the request (check UA)"
    assert response.status_code == 200
    assert response.body.get("message")


def test_econfirm_live_kes100_fund_and_reverse_once() -> None:
    if os.getenv("ECONFIRM_LIVE_MONEY_CONFIRM", "") != "KES100":
        pytest.skip("Moves real money. Set ECONFIRM_LIVE_MONEY_CONFIRM=KES100 to run it.")
    payer_phone = os.getenv("ECONFIRM_LIVE_PAYER_PHONE", "").strip()
    receiver_phone = os.getenv("ECONFIRM_LIVE_RECEIVER_PHONE", "").strip()
    if not payer_phone or not receiver_phone:
        pytest.skip("Set ECONFIRM_LIVE_PAYER_PHONE and ECONFIRM_LIVE_RECEIVER_PHONE.")

    settings = _settings()
    if not settings.econfirm_fallback_email.strip():
        pytest.skip("Set ECONFIRM_FALLBACK_EMAIL (eConfirm requires party e-mails).")
    provider = build_econfirm_provider(settings)

    record = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id=f"VK-LIVE-{int(time.time())}",
            buyer_id="live-smoke-buyer",
            seller_id="live-smoke-seller",
            amount=LIVE_AMOUNT,
            currency="KES",
            receiver_phone=receiver_phone,
            description="Vekro M20 live smoke test (KES 100, reversed to payer)",
            terms="Smoke test. Funds are reversed to the payer once funded.",
        )
    )
    print(f"\neConfirm live escrow: {record.escrow_reference}")
    assert record.escrow_reference
    assert record.confirmation_code, "create must return the one-time confirmation_code"

    funding = provider.request_funding(
        FundingRequest(
            escrow_reference=record.escrow_reference,
            amount=LIVE_AMOUNT,
            phone_number=payer_phone,
            account_reference=record.transaction_id,
        )
    )
    assert funding.outcome != CollectionOutcome.FAILED_DEFINITE, funding.message
    print("STK prompt sent; approve it on the payer phone.")

    funded = _poll(
        lambda: (
            (status := provider.get_funding_status(record.escrow_reference)).outcome
            == CollectionOutcome.SUCCEEDED,
            status,
        ),
        timeout_seconds=FUNDING_WAIT_SECONDS,
    )
    assert funded[0], f"escrow not funded in time (last status {funded[1].raw_status})"

    reversal = provider.refund(
        PayoutRequest(
            escrow_reference=record.escrow_reference,
            amount=LIVE_AMOUNT,
            destination_phone=payer_phone,
            purpose="live-smoke-reversal",
            confirmation_code=record.confirmation_code,
            notes="Vekro M20 live smoke test reversal",
        )
    )
    assert reversal.outcome != PayoutOutcome.FAILED_DEFINITE, reversal.message

    settled = _poll(
        lambda: (
            (status := provider.get_payout_status(record.escrow_reference)).outcome
            != PayoutOutcome.UNKNOWN,
            status,
        ),
        timeout_seconds=PAYOUT_WAIT_SECONDS,
    )
    assert settled[0], f"reversal still in progress (last status {settled[1].raw_status})"
    assert settled[1].outcome == PayoutOutcome.SUCCEEDED, settled[1].raw_status
