from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import User
from app.schemas.auth import RegisterUserRequest


class PhoneAlreadyRegisteredError(Exception):
    pass


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
        message = str(getattr(exc, "orig", exc)).lower()
        if "phone" in message and "duplicate" in message:
            raise PhoneAlreadyRegisteredError from exc
        raise

    db.refresh(user)
    return user
