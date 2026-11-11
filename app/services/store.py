"""A member's store (M28, docs/accounts-and-modes.md): selling is a capability of the account."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.user import SELLER_TERMS_VERSION, StoreStatus, User, UserRole


class StoreNotAllowedError(Exception):
    """Admins never hold a store."""


def open_store_on(
    user: User, *, now: datetime | None = None, terms_version: str = SELLER_TERMS_VERSION
) -> User:
    """Mark the account's store open, with the accepted seller terms. The caller commits."""
    if user.role == UserRole.ADMIN:
        raise StoreNotAllowedError("Admin accounts can't open a store.")
    current = now or datetime.now(UTC)
    user.store_status = StoreStatus.ACTIVE
    user.store_opened_at = current
    user.store_closed_at = None
    user.seller_terms_version = terms_version
    user.seller_terms_accepted_at = current
    return user
