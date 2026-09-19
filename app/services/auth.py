from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_hashed_token,
    verify_password,
)
from app.core.settings import get_settings
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.auth import LoginRequest, RegisterUserRequest

UNIQUE_VIOLATION_SQLSTATE = "23505"
PHONE_UNIQUE_CONSTRAINT_NAMES = {"ix_users_phone", "users_phone_key"}
_LOGIN_RATE_LIMIT_BUCKETS: dict[str, deque[datetime]] = {}
_LOGIN_RATE_LIMIT_LOCK = Lock()


class PhoneAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class TooManyLoginAttemptsError(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many login attempts")
        self.retry_after_seconds = retry_after_seconds


class AccountLockedError(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Account is temporarily locked")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class ActiveSessionContext:
    user: User
    session: UserSession


def _is_phone_unique_violation(exc: IntegrityError) -> bool:
    original_error = getattr(exc, "orig", None)
    if original_error is None:
        return False

    sqlstate = getattr(original_error, "sqlstate", None) or getattr(original_error, "pgcode", None)
    if sqlstate != UNIQUE_VIOLATION_SQLSTATE:
        return False

    diagnostics = getattr(original_error, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    if constraint_name in PHONE_UNIQUE_CONSTRAINT_NAMES:
        return True

    # Fallback for adapters/drivers that do not expose constraint diagnostics.
    message = str(original_error).lower()
    return "phone" in message and "duplicate" in message


def register_user(db: Session, payload: RegisterUserRequest) -> User:
    user = User(
        name=payload.name.strip(),
        phone=payload.phone,
        role=payload.role,
        password_hash=hash_password(payload.password),
        mpesa_phone=payload.mpesa_phone,
        mpesa_account_name=payload.mpesa_account_name,
    )

    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_phone_unique_violation(exc):
            raise PhoneAlreadyRegisteredError from exc
        raise

    db.refresh(user)
    return user


def _token_hash(token: str) -> str:
    return hash_session_token(token, get_settings().secret_key)


def _rate_limit_key(client_identifier: str, phone: str) -> str:
    return f"{client_identifier}|{phone}"


def _check_login_rate_limit(client_identifier: str, phone: str) -> int | None:
    settings = get_settings()
    now = datetime.now(UTC)
    window = timedelta(seconds=settings.login_rate_limit_window_seconds)
    key = _rate_limit_key(client_identifier, phone)

    with _LOGIN_RATE_LIMIT_LOCK:
        bucket = _LOGIN_RATE_LIMIT_BUCKETS.setdefault(key, deque())

        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= settings.login_rate_limit_max_attempts:
            retry_after = int((bucket[0] + window - now).total_seconds())
            return max(retry_after, 1)

        bucket.append(now)

    return None


def _clear_login_rate_limit(client_identifier: str, phone: str) -> None:
    key = _rate_limit_key(client_identifier, phone)
    with _LOGIN_RATE_LIMIT_LOCK:
        _LOGIN_RATE_LIMIT_BUCKETS.pop(key, None)


def _register_failed_user_login(db: Session, user: User, now: datetime) -> None:
    settings = get_settings()
    user.failed_login_attempts += 1

    if user.failed_login_attempts >= settings.login_lockout_max_attempts:
        user.locked_until = now + timedelta(seconds=settings.login_lockout_seconds)
        user.failed_login_attempts = 0
        db.add(user)
        db.commit()
        raise AccountLockedError(settings.login_lockout_seconds)

    db.add(user)
    db.commit()


def _ensure_user_not_locked(user: User, now: datetime) -> None:
    if user.locked_until is None:
        return
    if user.locked_until <= now:
        return

    retry_after = int((user.locked_until - now).total_seconds())
    raise AccountLockedError(max(retry_after, 1))


def _reset_user_lockout_state(db: Session, user: User) -> None:
    if user.failed_login_attempts == 0 and user.locked_until is None:
        return

    user.failed_login_attempts = 0
    user.locked_until = None
    db.add(user)
    db.flush()


def authenticate_and_create_session(
    db: Session,
    payload: LoginRequest,
    client_identifier: str,
) -> AuthenticatedSession:
    retry_after = _check_login_rate_limit(client_identifier, payload.phone)
    if retry_after is not None:
        raise TooManyLoginAttemptsError(retry_after)

    user = db.execute(select(User).where(User.phone == payload.phone)).scalar_one_or_none()
    now = datetime.now(UTC)
    if user is not None:
        _ensure_user_not_locked(user, now)

    if user is None or not verify_password(payload.password, user.password_hash):
        if user is not None:
            _register_failed_user_login(db, user, now)
        raise InvalidCredentialsError

    if not user.is_active:
        raise InvalidCredentialsError

    _reset_user_lockout_state(db, user)

    session_token = generate_session_token()
    csrf_token = generate_session_token()
    expires_at = now + timedelta(seconds=get_settings().session_cookie_max_age_seconds)

    db_session = UserSession(
        user_id=user.id,
        session_token_hash=_token_hash(session_token),
        csrf_token_hash=_token_hash(csrf_token),
        expires_at=expires_at,
    )
    db.add(db_session)
    db.commit()

    _clear_login_rate_limit(client_identifier, payload.phone)

    return AuthenticatedSession(
        user=user,
        session_token=session_token,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


def resolve_active_session(db: Session, session_token: str) -> ActiveSessionContext | None:
    row = db.execute(
        select(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .where(UserSession.session_token_hash == _token_hash(session_token))
    ).one_or_none()
    if row is None:
        return None

    session, user = row
    now = datetime.now(UTC)
    if session.revoked_at is not None:
        return None
    if session.expires_at <= now:
        session.revoked_at = now
        db.add(session)
        db.commit()
        return None
    if not user.is_active:
        return None

    return ActiveSessionContext(user=user, session=session)


def is_valid_csrf_for_session(csrf_token: str, session: UserSession) -> bool:
    return verify_hashed_token(csrf_token, session.csrf_token_hash, get_settings().secret_key)


def invalidate_session(db: Session, session_token: str | None) -> bool:
    if not session_token:
        return False

    session = db.execute(
        select(UserSession).where(UserSession.session_token_hash == _token_hash(session_token))
    ).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        return False

    session.revoked_at = datetime.now(UTC)
    db.add(session)
    db.commit()
    return True
