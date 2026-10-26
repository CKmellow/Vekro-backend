"""Operator commands. Run with `python -m app.cli <command>`.

create-admin: create a support admin, or promote an existing account to admin. The password comes
from the VEKRO_ADMIN_PASSWORD environment variable, never a flag, so it stays out of shell history.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_phone
from app.core.security import hash_password
from app.models.user import User, UserRole

PASSWORD_ENV = "VEKRO_ADMIN_PASSWORD"


def create_admin(db: Session, *, phone: str, name: str, password: str) -> tuple[User, bool]:
    """The admin, and whether it was created (False: an existing account was promoted)."""
    if len(password) < 12:
        raise ValueError("Admin passwords need at least 12 characters.")
    normalized = normalize_phone(phone)
    user = db.execute(select(User).where(User.phone == normalized)).scalar_one_or_none()
    created = user is None
    if user is None:
        user = User(name=name.strip(), phone=normalized, role=UserRole.ADMIN, password_hash="")
        db.add(user)
    user.role = UserRole.ADMIN
    user.password_hash = hash_password(password)
    user.is_active = True
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    db.refresh(user)
    return user, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="Create or promote a support admin.")
    admin.add_argument("--phone", required=True)
    admin.add_argument("--name", default="Vekro Support")
    args = parser.parse_args(argv)

    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        print(f"Set {PASSWORD_ENV} to the admin password.", file=sys.stderr)
        return 2

    from app.db.session import _get_session_factory

    with _get_session_factory()() as db:
        try:
            user, created = create_admin(db, phone=args.phone, name=args.name, password=password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"{'Created' if created else 'Promoted'} admin {user.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
