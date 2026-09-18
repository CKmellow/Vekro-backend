from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import User
from app.schemas.auth import RegisterUserRequest

UNIQUE_VIOLATION_SQLSTATE = "23505"
PHONE_UNIQUE_CONSTRAINT_NAMES = {"ix_users_phone", "users_phone_key"}


class PhoneAlreadyRegisteredError(Exception):
    pass


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
