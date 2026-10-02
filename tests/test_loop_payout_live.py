import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.core.settings import get_settings
from app.services.custody.dto import PayoutRequest
from app.services.custody.loop_auth import LoopTokenManager
from app.services.custody.loop_payout import LoopPayoutRail

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.getenv("RUN_LOOP_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
    reason="Opt-in live test. Set RUN_LOOP_LIVE_TESTS=1 to enable.",
)
def test_loop_live_payout_sandbox_smoke() -> None:
    settings = get_settings()
    if not settings.loop_enabled:
        pytest.skip("LOOP rail is disabled in environment settings.")

    destination_phone = os.getenv("LOOP_LIVE_DESTINATION_PHONE", "").strip()
    if not destination_phone:
        pytest.skip("Set LOOP_LIVE_DESTINATION_PHONE for live payout smoke test.")

    token_manager = LoopTokenManager(
        base_url=settings.loop_base_url,
        client_id=settings.loop_client_id,
        client_secret=settings.loop_client_secret,
    )
    rail = LoopPayoutRail(
        base_url=settings.loop_base_url,
        shortcode=settings.loop_shortcode,
        passkey=settings.loop_passkey,
        token_manager=token_manager,
    )

    purpose = f"live-loop-smoke-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    result = rail.request_payout(
        PayoutRequest(
            escrow_reference=purpose,
            amount=Decimal("1.00"),
            destination_phone=destination_phone,
            purpose=purpose,
            currency="KES",
        )
    )

    assert result.provider_reference
