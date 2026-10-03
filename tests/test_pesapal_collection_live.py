from __future__ import annotations

import os

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
