from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.core.settings import get_settings
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalTokenManager
from app.services.custody.pesapal_collection import PesapalCollectionRail


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_PESAPAL_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
    reason="Opt-in live test. Set RUN_PESAPAL_LIVE_TESTS=1 to enable.",
)
def test_pesapal_live_auth_and_optional_status_smoke() -> None:
    settings = get_settings()
    if not settings.pesapal_enabled:
        pytest.skip("PESAPAL_ENABLED is false in environment settings.")

    manager = PesapalTokenManager(
        base_url=settings.pesapal_base_url,
        consumer_key=settings.pesapal_consumer_key,
        consumer_secret=settings.pesapal_consumer_secret,
    )
    token = manager.get_access_token()
    assert token

    provider_reference = os.getenv("PESAPAL_LIVE_ORDER_TRACKING_ID", "").strip()
    if not provider_reference:
        pytest.skip(
            "Set PESAPAL_LIVE_ORDER_TRACKING_ID to run live status inquiry in this smoke test."
        )

    rail = PesapalCollectionRail(
        base_url=settings.pesapal_base_url,
        callback_url=settings.pesapal_callback_url,
        ipn_id=settings.pesapal_ipn_id,
        token_manager=manager,
    )
    result = rail.get_funding_status(provider_reference)

    assert result.provider_reference
    assert result.outcome in {
        CollectionOutcome.SUCCEEDED,
        CollectionOutcome.FAILED_DEFINITE,
        CollectionOutcome.UNKNOWN,
    }


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_PESAPAL_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
    reason="Opt-in live test. Set RUN_PESAPAL_LIVE_TESTS=1 to enable.",
)
def test_pesapal_live_card_checkout_opens_and_reads_as_pending() -> None:
    """#251: a real sandbox order (KES 1, nobody pays it) gets a hosted-checkout URL, and its
    status is pending, not failed. Needs PESAPAL_* with an IPN id, and FRONTEND_URL."""
    from app.services.card_payments import build_rail, card_payments_available
    from app.services.custody.dto import FundingRequest

    settings = get_settings().model_copy(update={"pesapal_enabled": True})
    if not card_payments_available(settings):
        pytest.skip("Card payments need PESAPAL_* (with PESAPAL_IPN_ID) and FRONTEND_URL.")
    reference = f"VK-LIVE{int(time.time()) % 100000}"
    order = SimpleNamespace(id=uuid.uuid4(), reference=reference)
    rail = build_rail(settings, transaction=order)

    opened = rail.request_funding(
        FundingRequest(
            escrow_reference=reference,
            amount=Decimal("1.00"),
            phone_number="+254700000000",
            account_reference=reference,
        )
    )
    assert opened.outcome == CollectionOutcome.SUCCEEDED
    assert str(opened.metadata["redirect_url"]).startswith("https://")

    status = rail.get_funding_status(opened.provider_reference)
    assert status.outcome == CollectionOutcome.UNKNOWN
