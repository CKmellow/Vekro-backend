from __future__ import annotations

import os
import uuid

import pytest
from app.services.sms import AfricasTalkingSmsSender, SmsOutcome

pytestmark = pytest.mark.live


@pytest.mark.skipif(
    os.getenv("RUN_AFRICASTALKING_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
    reason="Opt-in live test. Set RUN_AFRICASTALKING_LIVE_TESTS=1 to enable.",
)
def test_africastalking_live_sms_send_smoke() -> None:
    api_key = os.getenv("AFRICASTALKING_API_KEY", "").strip()
    if not api_key:
        pytest.skip("Set AFRICASTALKING_API_KEY for live SMS smoke test.")

    destination_phone = os.getenv("AFRICASTALKING_LIVE_DESTINATION_PHONE", "").strip()
    if not destination_phone:
        pytest.skip("Set AFRICASTALKING_LIVE_DESTINATION_PHONE for live SMS smoke test.")

    sender = AfricasTalkingSmsSender(
        username=os.getenv("AFRICASTALKING_USERNAME", "sandbox"),
        api_key=api_key,
        sender_id=os.getenv("AFRICASTALKING_SENDER_ID", ""),
        max_attempts=1,
    )
    result = sender.send(f"Vekro SMS smoke {uuid.uuid4().hex[:8]}", [destination_phone])

    assert result.all_sent is True
    assert len(result.recipients) == 1
    recipient = result.recipients[0]
    assert recipient.outcome == SmsOutcome.SENT
    assert recipient.status_code in {100, 101, 102}
    assert recipient.message_id
