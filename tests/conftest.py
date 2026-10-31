"""Suite-wide test settings."""

import os

# Every status change, ledger row and money audit row must have its anchor event: in strict mode
# a missing one raises instead of being logged and backfilled (M21 A3, decision D2).
os.environ.setdefault("ANCHOR_STRICT_EVENTS", "true")
