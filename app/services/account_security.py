"""Password change and reset, and signed-in devices (frontend gap #149)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.models.user_session import UserSession
from app.services.notification_delivery import Dispatcher, run_inline
from app.services.phone_auth import (
    IssuedCode,
    PhoneCodeExpiredError,
    consume_phone_code,
    request_phone_code,
)


class WrongPasswordError(Exception):
    pass


class SessionNotFoundError(Exception):
    pass


class CurrentSessionError(Exception):
    """Use log out for this device."""


@dataclass(frozen=True)
class DeviceSession:
    id: uuid.UUID
    device: str
    created_at: datetime
    last_seen_at: datetime
    current: bool


_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Android", "Android"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Windows", "Windows"),
    ("Mac OS X", "Mac"),
    ("Linux", "Linux"),
)


def device_label(user_agent: str | None) -> str:
    """ "Chrome on Android" from a User-Agent; "Unknown device" when there's nothing to read."""
    ua = user_agent or ""
    browser = next((name for key, name in _BROWSERS if key in ua), None)
    system = next((name for key, name in _SYSTEMS if re.search(re.escape(key), ua)), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"


def _revoke(db: Session, user_id: uuid.UUID, *, keep: uuid.UUID | None, now: datetime) -> int:
    query = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if keep is not None:
        query = query.where(UserSession.id != keep)
    return int(db.execute(query.values(revoked_at=now)).rowcount or 0)  # type: ignore[attr-defined]


def change_password(
    db: Session, user: User, *, current_password: str, new_password: str, keep_session: uuid.UUID
) -> int:
    """Change the password and sign out every other device. Returns how many were signed out."""
    if not verify_password(current_password, user.password_hash):
        raise WrongPasswordError
    now = datetime.now(UTC)
    user.password_hash = hash_password(new_password)
    revoked = _revoke(db, user.id, keep=keep_session, now=now)
    db.commit()
    return revoked


def request_reset_code(
    db: Session, phone: str, *, client_identifier: str, dispatch: Dispatcher = run_inline
) -> IssuedCode | None:
    """Text a reset code if the phone has an account. None (and no SMS) otherwise, so the API can
    answer the same either way and not reveal who has an account."""
    if db.execute(select(User.id).where(User.phone == phone)).first() is None:
        return None
    return request_phone_code(
        db, phone, purpose="reset", client_identifier=client_identifier, dispatch=dispatch
    )


def reset_password(db: Session, phone: str, code: str, new_password: str) -> None:
    """Set a new password with the SMS code and sign out every device."""
    user = db.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
    consume_phone_code(db, phone, code, "reset")
    if user is None:
        db.rollback()
        raise PhoneCodeExpiredError
    user.password_hash = hash_password(new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    _revoke(db, user.id, keep=None, now=datetime.now(UTC))
    db.commit()


def list_sessions(db: Session, user_id: uuid.UUID, current: uuid.UUID) -> list[DeviceSession]:
    now = datetime.now(UTC)
    rows = (
        db.execute(
            select(UserSession).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    out: list[DeviceSession] = []
    for row in rows:
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        if expires <= now:
            continue
        seen = row.last_seen_at or row.created_at
        out.append(
            DeviceSession(
                id=row.id,
                device=device_label(row.user_agent),
                created_at=row.created_at,
                last_seen_at=seen,
                current=row.id == current,
            )
        )
    out.sort(key=lambda d: (not d.current, -_ts(d.last_seen_at)))
    return out


def _ts(value: datetime) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


def revoke_session(
    db: Session, user_id: uuid.UUID, session_id: uuid.UUID, current: uuid.UUID
) -> None:
    if session_id == current:
        raise CurrentSessionError
    row = db.get(UserSession, session_id)
    if row is None or row.user_id != user_id or row.revoked_at is not None:
        raise SessionNotFoundError
    row.revoked_at = datetime.now(UTC)
    db.commit()


def revoke_other_sessions(db: Session, user_id: uuid.UUID, current: uuid.UUID) -> int:
    revoked = _revoke(db, user_id, keep=current, now=datetime.now(UTC))
    db.commit()
    return revoked
