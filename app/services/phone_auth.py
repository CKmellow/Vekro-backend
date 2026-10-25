"""Buyer sign-in with a one-time SMS code (frontend gap #11).

A buyer asks for a 6-digit code, then verifies it to open the same cookie session as the password
login. Codes are random, single-use, short-lived and attempt-limited; only their HMAC is stored,
keyed by the phone so a code can't be replayed for another number. The code is never logged.
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.security import hash_password, hash_session_token, verify_hashed_token
from app.core.settings import get_settings
from app.models.phone_code import PhoneCode
from app.models.user import User, UserRole
from app.services.auth import AuthenticatedSession, create_session
from app.services.notification_delivery import (
    Dispatcher,
    SenderProvider,
    default_sender_provider,
    run_inline,
)

CODE_LENGTH = 6
Purpose = Literal["sign_in", "payout"]
NEW_BUYER_NAME = "Vekro buyer"

logger = logging.getLogger("app.auth.phone")

# Codes asked for by one client across all numbers, per hour (in memory, like the login limiter).
CLIENT_HOURLY_LIMIT = 20
_CLIENT_BUCKETS: dict[str, deque[datetime]] = {}
_CLIENT_LOCK = Lock()


class PhoneCodeRateLimitedError(Exception):
    """Asked too soon after the last code, too often this hour, or locked after wrong tries."""

    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many sign-in codes")
        self.retry_after_seconds = max(int(retry_after_seconds), 1)


class PhoneCodeInvalidError(Exception):
    def __init__(self, attempts_left: int):
        super().__init__("Code does not match")
        self.attempts_left = attempts_left


class PhoneCodeExpiredError(Exception):
    """No active code for the phone: it expired, was used, or was never sent."""


class PhoneNotBuyerError(Exception):
    """The number belongs to a seller or admin, who log in with a password."""


@dataclass(frozen=True)
class IssuedCode:
    expires_at: datetime
    resend_after_seconds: int
    code: str


def _code_hash(phone: str, code: str, purpose: Purpose) -> str:
    # The purpose is in the hash too, so a sign-in code can never confirm a payout number.
    return hash_session_token(f"{purpose}:{phone}:{code}", get_settings().secret_key)


def _latest_code(db: Session, phone: str, purpose: Purpose) -> PhoneCode | None:
    return db.execute(
        select(PhoneCode)
        .where(PhoneCode.phone == phone, PhoneCode.purpose == purpose)
        .order_by(PhoneCode.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _aware(value: datetime) -> datetime:
    # SQLite drops the timezone; PostgreSQL keeps it.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _check_client_quota(client_identifier: str, current: datetime) -> None:
    """Stops one client from sending codes to many numbers (SMS pumping)."""
    cutoff = current - timedelta(hours=1)
    with _CLIENT_LOCK:
        bucket = _CLIENT_BUCKETS.setdefault(client_identifier, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= CLIENT_HOURLY_LIMIT:
            raise PhoneCodeRateLimitedError(
                (bucket[0] + timedelta(hours=1) - current).total_seconds()
            )
        bucket.append(current)


def sign_in_sms_text(code: str, ttl_seconds: int) -> str:
    minutes = max(ttl_seconds // 60, 1)
    return (
        f"Vekro: your sign-in code is {code}. It expires in {minutes} minutes. "
        "Never share it, not even with a seller or courier."
    )


def payout_sms_text(code: str, ttl_seconds: int) -> str:
    minutes = max(ttl_seconds // 60, 1)
    return (
        f"Vekro: your code to make this your payout number is {code}. It expires in {minutes} "
        "minutes. If you didn't ask for it, ignore this message."
    )


def request_phone_code(
    db: Session,
    phone: str,
    *,
    purpose: Purpose = "sign_in",
    client_identifier: str = "unknown",
    now: datetime | None = None,
    sender_provider: SenderProvider | None = None,
    dispatch: Dispatcher = run_inline,
) -> IssuedCode:
    settings = get_settings()
    current = now or datetime.now(UTC)

    latest = _latest_code(db, phone, purpose)
    if latest is not None:
        if latest.locked_until is not None and _aware(latest.locked_until) > current:
            raise PhoneCodeRateLimitedError((_aware(latest.locked_until) - current).total_seconds())
        resend_at = _aware(latest.created_at) + timedelta(
            seconds=settings.phone_code_resend_seconds
        )
        if latest.consumed_at is None and resend_at > current:
            raise PhoneCodeRateLimitedError((resend_at - current).total_seconds())

    hour_ago = current - timedelta(hours=1)
    sent_this_hour = db.execute(
        select(func.count())
        .select_from(PhoneCode)
        .where(PhoneCode.phone == phone, PhoneCode.created_at > hour_ago)
    ).scalar_one()
    if sent_this_hour >= settings.phone_code_hourly_limit:
        oldest = db.execute(
            select(func.min(PhoneCode.created_at)).where(
                PhoneCode.phone == phone, PhoneCode.created_at > hour_ago
            )
        ).scalar_one()
        raise PhoneCodeRateLimitedError(
            (_aware(oldest) + timedelta(hours=1) - current).total_seconds()
        )

    _check_client_quota(client_identifier, current)

    # A new code replaces any earlier one.
    db.execute(
        update(PhoneCode)
        .where(
            PhoneCode.phone == phone,
            PhoneCode.purpose == purpose,
            PhoneCode.consumed_at.is_(None),
        )
        .values(consumed_at=current)
    )
    code = f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"
    expires_at = current + timedelta(seconds=settings.phone_code_ttl_seconds)
    db.add(
        PhoneCode(
            phone=phone,
            purpose=purpose,
            code_hash=_code_hash(phone, code, purpose),
            expires_at=expires_at,
            created_at=current,
        )
    )
    db.commit()

    text = (sign_in_sms_text if purpose == "sign_in" else payout_sms_text)(
        code, settings.phone_code_ttl_seconds
    )
    dispatch(_send_job(phone, text, sender_provider or default_sender_provider))
    return IssuedCode(
        expires_at=expires_at,
        resend_after_seconds=settings.phone_code_resend_seconds,
        code=code,
    )


def _send_job(phone: str, text: str, sender_provider: SenderProvider) -> Callable[[], None]:
    def job() -> None:
        sender = sender_provider()
        if sender is None:
            logger.info("phone_code_sms_skipped reason=no_gateway")
            return
        try:
            result = sender.send(text, [phone])
            logger.info("phone_code_sms sent=%s", result.all_sent)
        except Exception as exc:
            logger.warning("phone_code_sms_error error_type=%s", type(exc).__name__)

    return job


def consume_phone_code(
    db: Session,
    phone: str,
    code: str,
    purpose: Purpose,
    *,
    now: datetime | None = None,
) -> None:
    """Check a code and use it up; wrong tries count down to a lockout. Success isn't committed."""
    settings = get_settings()
    current = now or datetime.now(UTC)

    latest = _latest_code(db, phone, purpose)
    if latest is not None and latest.locked_until is not None:
        if _aware(latest.locked_until) > current:
            raise PhoneCodeRateLimitedError((_aware(latest.locked_until) - current).total_seconds())
    if latest is None or latest.consumed_at is not None or _aware(latest.expires_at) <= current:
        raise PhoneCodeExpiredError

    if not verify_hashed_token(f"{purpose}:{phone}:{code}", latest.code_hash, settings.secret_key):
        latest.attempts += 1
        if latest.attempts >= settings.phone_code_max_attempts:
            latest.consumed_at = current
            latest.locked_until = current + timedelta(seconds=settings.phone_code_lockout_seconds)
            db.commit()
            raise PhoneCodeRateLimitedError(settings.phone_code_lockout_seconds)
        db.commit()
        raise PhoneCodeInvalidError(settings.phone_code_max_attempts - latest.attempts)

    latest.consumed_at = current


def verify_phone_code(
    db: Session,
    phone: str,
    code: str,
    *,
    now: datetime | None = None,
) -> AuthenticatedSession:
    current = now or datetime.now(UTC)
    user = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
    consume_phone_code(db, phone, code, "sign_in", now=current)
    if user is not None and user.role != UserRole.BUYER:
        db.rollback()
        raise PhoneNotBuyerError
    if user is not None and not user.is_active:
        db.rollback()
        raise PhoneCodeExpiredError

    if user is None:
        # No password: an unusable random one, so this account can only sign in by SMS code.
        user = User(
            name=NEW_BUYER_NAME,
            phone=phone,
            role=UserRole.BUYER,
            password_hash=hash_password(secrets.token_urlsafe(32)),
        )
        db.add(user)
        db.flush()
    return create_session(db, user, current)
