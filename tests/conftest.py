"""Suite-wide test settings."""

import os

# Every status change, ledger row and money audit row must have its anchor event: in strict mode
# a missing one raises instead of being logged and backfilled (M21 A3, decision D2).
os.environ.setdefault("ANCHOR_STRICT_EVENTS", "true")


# #241: no anchored payload, in any test, may carry a phone number or an e-mail address.
import pytest  # noqa: E402
from app.models.anchor_event import AnchorEvent  # noqa: E402
from sqlalchemy import event  # noqa: E402

from tests.pii_guard import pii_findings  # noqa: E402


@event.listens_for(AnchorEvent, "before_insert", propagate=True)
def _refuse_personal_data_in_payloads(_mapper, _connection, target: AnchorEvent) -> None:
    findings = pii_findings(target.payload)
    if findings:
        pytest.fail(f"{target.event_type} payload carries personal data: {findings}")
