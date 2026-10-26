from typing import cast

import pytest
from app.cli import create_admin, main
from app.core.security import verify_password
from app.db.base import Base
from app.models.user import User, UserRole
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[cast(Table, User.__table__)])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def test_creates_an_admin(db: Session) -> None:
    user, created = create_admin(db, phone="0700000099", name="Support", password="long-password-1")
    assert created and user.role == UserRole.ADMIN and user.phone == "+254700000099"
    assert verify_password("long-password-1", user.password_hash)


def test_promotes_an_existing_account_and_clears_lockout(db: Session) -> None:
    db.add(
        User(
            name="Ops",
            phone="+254700000098",
            role=UserRole.SELLER,
            password_hash="x",
            failed_login_attempts=4,
        )
    )
    db.commit()
    user, created = create_admin(db, phone="+254700000098", name="Ops", password="long-password-2")
    assert not created and user.role == UserRole.ADMIN and user.failed_login_attempts == 0


def test_rejects_short_passwords(db: Session) -> None:
    with pytest.raises(ValueError):
        create_admin(db, phone="0700000097", name="S", password="short")


def test_needs_the_password_from_the_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("VEKRO_ADMIN_PASSWORD", raising=False)
    assert main(["create-admin", "--phone", "0700000096"]) == 2
    assert "VEKRO_ADMIN_PASSWORD" in capsys.readouterr().err
