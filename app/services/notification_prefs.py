"""Seller notification preferences (frontend gap #150): in-app and SMS per category.

Only sellers can mute anything. Buyers always get every message, because one of them carries the
delivery code. Support decisions always show in the app, so a decision about money can't be missed.
"""

from __future__ import annotations

from typing import Literal

from app.models.notification import NotificationEventType as E
from app.models.user import User, UserRole

Category = Literal["payment_received", "delivery", "returns", "support"]
Channel = Literal["in_app", "sms"]
CATEGORIES: tuple[Category, ...] = ("payment_received", "delivery", "returns", "support")
LOCKED: set[tuple[Category, Channel]] = {("support", "in_app")}

CATEGORY_BY_EVENT: dict[E, Category] = {
    E.TRANSACTION_CREATED: "payment_received",
    E.TRANSACTION_LOCKED: "payment_received",
    E.OTP_GIVEN: "payment_received",
    E.TRANSACTION_DISPATCHED: "delivery",
    E.DELIVERY_ARRIVED: "delivery",
    E.SYSTEM_TIMEOUT: "delivery",
    E.OTP_WITHHELD: "returns",
    E.DISPUTE_OPENED: "returns",
    E.DISPUTE_UPDATED: "returns",
    E.ADMIN_DECISION: "support",
}


def preferences(user: User) -> dict[Category, dict[Channel, bool]]:
    stored = user.notification_prefs or {}
    out: dict[Category, dict[Channel, bool]] = {}
    for category in CATEGORIES:
        saved = stored.get(category) or {}
        out[category] = {
            channel: True if (category, channel) in LOCKED else bool(saved.get(channel, True))
            for channel in ("in_app", "sms")
        }
    return out


def set_preferences(user: User, wanted: dict[str, dict[str, bool]]) -> None:
    current = preferences(user)
    for category in CATEGORIES:
        for channel in ("in_app", "sms"):
            value = (wanted.get(category) or {}).get(channel)
            if value is not None and (category, channel) not in LOCKED:
                current[category][channel] = bool(value)
    user.notification_prefs = {k: dict(v) for k, v in current.items()}


def muted_event_types(user: User, channel: Channel) -> set[E]:
    if user.role != UserRole.SELLER or not user.notification_prefs:
        return set()
    prefs = preferences(user)
    return {event for event, category in CATEGORY_BY_EVENT.items() if not prefs[category][channel]}
