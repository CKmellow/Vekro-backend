"""Account fields for tests (M28): a buyer is a member, a seller is a member with an open store
and an SMS-verified payout number, an admin is an admin."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.user import PartyRole, StoreStatus, UserRole

BUYER = PartyRole.BUYER
SELLER = PartyRole.SELLER
ADMIN = PartyRole.ADMIN

_OPENED = datetime(2026, 1, 1, tzinfo=UTC)


def account_fields(role: Any) -> dict[str, Any]:
    """User(**account_fields(SELLER), ...) for a pre-M28 role (buyer, seller or admin)."""
    value = getattr(role, "value", role)
    if value == "admin":
        return {"role": UserRole.ADMIN}
    if value == "seller":
        return {
            "role": UserRole.MEMBER,
            "store_status": StoreStatus.ACTIVE,
            "store_opened_at": _OPENED,
            "seller_terms_version": "test",
            "seller_terms_accepted_at": _OPENED,
            "payout_verified_at": _OPENED,
        }
    return {"role": UserRole.MEMBER, "store_status": StoreStatus.NONE}
