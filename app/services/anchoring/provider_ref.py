"""provider_ref: how a payment provider's reference is named outside the private tables (#241).

Provider references can carry personal data: Daraja's ``CheckoutRequestID`` ends with the payer's
subscriber number (docs/daraja-observed.md). Both parties can read an order's anchored payloads,
so a raw reference never goes into ``money_audit_events``, an anchored payload, a log line or a
notification. They carry ``provider_ref = hex(HMAC-SHA256(key, "provider:" + raw))`` instead,
keyed with ``ANCHOR_ADMIN_REF_SECRET`` (newest key first, like ``admin_ref``). The raw value stays
only where Vekro needs it to talk to the provider: payment_prompts, collection_attempts,
payout_attempts, provider_events and the escrow's custody columns.
"""

from __future__ import annotations

import hashlib
import hmac

from app.services.anchoring.admin_ref import configured_keys

_DOMAIN = "provider:"


def provider_ref(raw: str | None, *, keys: list[str] | None = None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    key = (keys or configured_keys())[0]
    message = (_DOMAIN + str(raw).strip()).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def candidate_refs(raw: str, *, keys: list[str] | None = None) -> list[str]:
    """Every ref ``raw`` may have been stored under (one per configured key), for lookups."""
    return [ref for key in keys or configured_keys() if (ref := provider_ref(raw, keys=[key]))]


def stable_digest(raw: str) -> str:
    """Unkeyed digest for idempotency keys, which must survive key rotation. Only for references
    that carry no personal data (the simulated rail's own synthetic references)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
