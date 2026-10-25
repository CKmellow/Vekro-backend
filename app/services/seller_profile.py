"""Seller shop profile, logo and payout number (frontend gap #144)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.models.user import User
from app.services.notification_delivery import (
    Dispatcher,
    SenderProvider,
    default_sender_provider,
    run_inline,
)
from app.services.phone_auth import IssuedCode, consume_phone_code, request_phone_code

LOGO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_LOGO_BYTES = 1024 * 1024

logger = logging.getLogger("app.seller.profile")


class LogoRejectedError(Exception):
    def __init__(self, message: str, *, too_large: bool = False):
        super().__init__(message)
        self.too_large = too_large


class PayoutCodeRequiredError(Exception):
    """A new payout number needs the SMS code sent to it."""


def payout_phone(user: User) -> str:
    return user.mpesa_phone or user.phone


def has_orders(db: Session, seller_id: uuid.UUID) -> bool:
    return bool(db.execute(select(exists().where(Transaction.seller_id == seller_id))).scalar())


def update_profile(db: Session, user: User, changes: dict[str, str | None]) -> User:
    for field in ("shop_name", "shop_area", "shop_about"):
        if field in changes:
            value = changes[field]
            setattr(user, field, value.strip() or None if isinstance(value, str) else None)
    db.commit()
    db.refresh(user)
    return user


def set_logo(db: Session, user: User, content_type: str, data: bytes) -> User:
    media_type = content_type.split(";")[0].strip().lower()
    if media_type not in LOGO_CONTENT_TYPES:
        raise LogoRejectedError("Logos must be JPEG, PNG or WebP.")
    if not data:
        raise LogoRejectedError("The logo is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoRejectedError("Logos must be 1 MB or smaller.", too_large=True)
    user.logo_content_type = media_type
    user.logo_data = data
    user.logo_updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def clear_logo(db: Session, user: User) -> User:
    user.logo_content_type = None
    user.logo_data = None
    user.logo_updated_at = None
    db.commit()
    db.refresh(user)
    return user


def request_payout_code(
    db: Session,
    phone: str,
    *,
    client_identifier: str,
    dispatch: Dispatcher = run_inline,
) -> IssuedCode:
    return request_phone_code(
        db, phone, purpose="payout", client_identifier=client_identifier, dispatch=dispatch
    )


def payout_changed_sms_text(new_phone: str) -> str:
    masked = f"{new_phone[:7]} *** {new_phone[-3:]}" if len(new_phone) >= 10 else "a new number"
    return (
        f"Vekro: your payout number was changed to {masked}. If this wasn't you, reply to this "
        "message or contact Vekro support now."
    )


def change_payout(
    db: Session,
    user: User,
    *,
    mpesa_phone: str,
    mpesa_account_name: str,
    code: str | None,
    sender_provider: SenderProvider | None = None,
    dispatch: Dispatcher = run_inline,
) -> User:
    """Set the payout number and name. A new number needs its SMS code once the seller has orders;
    before the first order (onboarding) no money can move, so it's set directly."""
    previous = payout_phone(user)
    changed = mpesa_phone != previous
    if changed and has_orders(db, user.id):
        if not code:
            raise PayoutCodeRequiredError
        consume_phone_code(db, mpesa_phone, code, "payout")

    user.mpesa_phone = mpesa_phone
    user.mpesa_account_name = mpesa_account_name.strip()
    db.commit()
    db.refresh(user)

    if changed:
        logger.info("payout_number_changed user_id=%s", user.id)
        dispatch(_send_job(previous, payout_changed_sms_text(mpesa_phone), sender_provider))
    return user


def _send_job(phone: str, text: str, sender_provider: SenderProvider | None) -> Callable[[], None]:
    def job() -> None:
        sender = (sender_provider or default_sender_provider)()
        if sender is None:
            return
        try:
            sender.send(text, [phone])
        except Exception as exc:
            logger.warning("payout_changed_sms_error error_type=%s", type(exc).__name__)

    return job
