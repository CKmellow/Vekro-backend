"""Profile & security in the buyer area (M28): name and sign-in phone.

A new sign-in phone is proven by an SMS code sent to it. Every other device is signed out and the
old number is told by SMS. The payout number of a store is separate and doesn't change here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.account_security import _revoke
from app.services.notification_delivery import (
    Dispatcher,
    SenderProvider,
    default_sender_provider,
    run_inline,
)
from app.services.phone_auth import IssuedCode, consume_phone_code, request_phone_code

logger = logging.getLogger("app.profile")


class PhoneTakenError(Exception):
    """Another account already signs in with this number."""


class SamePhoneError(Exception):
    """The new number is the one already in use."""


def update_name(db: Session, user: User, name: str) -> User:
    user.name = name.strip()
    db.commit()
    db.refresh(user)
    return user


def _check_new_phone(db: Session, user: User, phone: str) -> None:
    if phone == user.phone:
        raise SamePhoneError
    taken = db.execute(select(User.id).where(User.phone == phone)).first()
    if taken is not None:
        raise PhoneTakenError


def request_phone_change_code(
    db: Session,
    user: User,
    phone: str,
    *,
    client_identifier: str,
    dispatch: Dispatcher = run_inline,
) -> IssuedCode:
    _check_new_phone(db, user, phone)
    return request_phone_code(
        db, phone, purpose="phone_change", client_identifier=client_identifier, dispatch=dispatch
    )


def phone_changed_sms_text() -> str:
    return (
        "Vekro: your account now signs in with a different number. If this wasn't you, call "
        "Vekro support at once."
    )


def change_phone(
    db: Session,
    user: User,
    phone: str,
    code: str,
    *,
    keep_session: uuid.UUID | None,
    sender_provider: SenderProvider | None = None,
    dispatch: Dispatcher = run_inline,
) -> User:
    _check_new_phone(db, user, phone)
    consume_phone_code(db, phone, code, "phone_change")
    previous = user.phone
    user.phone = phone
    _revoke(db, user.id, keep=keep_session, now=datetime.now(UTC))
    db.commit()
    db.refresh(user)
    logger.info("sign_in_phone_changed user_id=%s", user.id)
    dispatch(_notify(previous, sender_provider or default_sender_provider))
    return user


def _notify(phone: str, sender_provider: SenderProvider) -> Callable[[], None]:
    def job() -> None:
        sender = sender_provider()
        if sender is None:
            return
        try:
            sender.send(phone_changed_sms_text(), [phone])
        except Exception as exc:  # pragma: no cover - the gateway's own failures
            logger.warning("phone_changed_sms_error error_type=%s", type(exc).__name__)

    return job
