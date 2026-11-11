"""M28 sign-in methods per role (docs/accounts-and-modes.md, section 3).

Members (no store, open store, closed store) use a password, an SMS code or Google. Admins use a
password, or Google only once they linked it themselves (test_google_auth.py), and never an SMS
code: not to sign in, not to reset a password. Their numbers get the same answer as any other
number, with no SMS and no stored code.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_password
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.phone_code import PhoneCode
from app.models.user import PartyRole, StoreStatus, User, UserRole
from app.models.user_session import UserSession
from app.schemas.auth import LoginRequest
from app.services import auth as auth_service
from app.services import phone_auth
from app.services.account_security import request_reset_code, reset_password
from app.services.auth import authenticate_and_create_session, resolve_active_session
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeRateLimitedError,
    request_phone_code,
    verify_phone_code,
)
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.accounts import account_fields

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
PASSWORD = "Mango-2026-pass"
PHONES = {
    "buyer": "+254712100001",
    "seller": "+254712100002",
    "closed": "+254712100003",
    "admin": "+254712100004",
}


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[list[str]] = []

    def send(self, message: str, recipients: Any) -> Any:
        self.sent.append(list(recipients))


@pytest.fixture
def factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [User.__table__, UserSession.__table__, PhoneCode.__table__]
    Base.metadata.create_all(engine, tables=[cast(Table, t) for t in tables])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def db(factory: sessionmaker) -> Session:
    session = factory()
    for who, phone in PHONES.items():
        role = (
            UserRole.ADMIN
            if who == "admin"
            else (PartyRole.BUYER if who == "buyer" else PartyRole.SELLER)
        )
        fields = account_fields(role)
        if who == "closed":
            fields["store_status"] = StoreStatus.CLOSED
        session.add(User(name=who, phone=phone, password_hash=hash_password(PASSWORD), **fields))
    session.commit()
    return session


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    phone_auth._CLIENT_BUCKETS.clear()
    phone_auth._DECOY_BUCKETS.clear()
    auth_service._LOGIN_RATE_LIMIT_BUCKETS.clear()
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)


def _codes(db: Session) -> int:
    return int(db.execute(select(func.count()).select_from(PhoneCode)).scalar_one())


@pytest.mark.parametrize("who", ["buyer", "seller", "closed", "admin"])
def test_every_role_signs_in_with_a_password(db: Session, who: str) -> None:
    session = authenticate_and_create_session(
        db, LoginRequest(phone=PHONES[who], password=PASSWORD), client_identifier=f"ip-{who}"
    )
    assert session.user.phone == PHONES[who]


@pytest.mark.parametrize("who", ["buyer", "seller", "closed"])
def test_every_member_signs_in_with_an_sms_code(db: Session, who: str) -> None:
    sender = FakeSender()
    issued = request_phone_code(db, PHONES[who], now=T0, sender_provider=lambda: sender)
    assert sender.sent == [[PHONES[who]]] and len(issued.code) == 6
    session = verify_phone_code(db, PHONES[who], issued.code, now=T0 + timedelta(minutes=1))
    assert session.user.phone == PHONES[who]
    # Single use.
    with pytest.raises(PhoneCodeExpiredError):
        verify_phone_code(db, PHONES[who], issued.code, now=T0 + timedelta(minutes=2))


def _refused_like_anyone(db: Session, phone: str, purpose: str = "sign_in") -> None:
    sender = FakeSender()
    real = request_phone_code(db, "+254712100099", purpose="sign_in", now=T0)
    issued = request_phone_code(
        db, phone, purpose=purpose, now=T0, sender_provider=lambda: sender  # type: ignore[arg-type]
    )
    # The same answer as a real code: expiry and resend timer; but no SMS and nothing stored.
    assert issued.expires_at == real.expires_at
    assert issued.resend_after_seconds == real.resend_after_seconds
    assert issued.code == "" and sender.sent == []
    assert db.execute(select(PhoneCode).where(PhoneCode.phone == phone)).first() is None
    # And the same throttles: a resend inside the cooldown is refused like a real one.
    with pytest.raises(PhoneCodeRateLimitedError):
        request_phone_code(db, phone, purpose=purpose, now=T0 + timedelta(seconds=5))  # type: ignore[arg-type]


def test_admins_never_get_a_sign_in_code(db: Session) -> None:
    _refused_like_anyone(db, PHONES["admin"])
    with pytest.raises(PhoneCodeExpiredError):
        verify_phone_code(db, PHONES["admin"], "123456", now=T0 + timedelta(minutes=1))
    assert db.execute(select(UserSession)).first() is None


def test_a_stray_code_row_still_never_opens_an_admin_session(db: Session) -> None:
    # Even if a sign-in code existed for an admin number (planted, or from before M28).
    db.add(
        PhoneCode(
            phone=PHONES["admin"],
            purpose="sign_in",
            code_hash=phone_auth._code_hash(PHONES["admin"], "424242", "sign_in"),
            expires_at=T0 + timedelta(minutes=5),
            created_at=T0,
        )
    )
    db.commit()
    with pytest.raises(PhoneCodeExpiredError):
        verify_phone_code(db, PHONES["admin"], "424242", now=T0 + timedelta(minutes=1))
    assert db.execute(select(UserSession)).first() is None


def test_admin_passwords_are_never_reset_by_sms(db: Session) -> None:
    _refused_like_anyone(db, PHONES["admin"], purpose="reset")
    db.add(
        PhoneCode(
            phone=PHONES["admin"],
            purpose="reset",
            code_hash=phone_auth._code_hash(PHONES["admin"], "434343", "reset"),
            expires_at=T0 + timedelta(minutes=5),
            created_at=T0,
        )
    )
    db.commit()
    with pytest.raises(PhoneCodeExpiredError):
        reset_password(db, PHONES["admin"], "434343", "a-new-admin-password")
    admin = db.execute(select(User).where(User.phone == PHONES["admin"])).scalar_one()
    assert admin.password_hash and admin.has_password


def test_unknown_numbers_get_the_same_reset_answer_and_throttle(db: Session) -> None:
    _refused_like_anyone(db, "+254712100077", purpose="reset")
    assert request_reset_code(db, "+254712100078", client_identifier="x").code == ""


def test_a_member_who_turned_sms_off_gets_no_code(db: Session) -> None:
    seller = db.execute(select(User).where(User.phone == PHONES["seller"])).scalar_one()
    seller.sms_login_enabled = False
    db.commit()
    _refused_like_anyone(db, PHONES["seller"])


@pytest.fixture
def client(factory: sessionmaker, db: Session, monkeypatch: pytest.MonkeyPatch):
    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("app.routers.auth.run_in_background", lambda job: job())
    monkeypatch.setattr("app.routers.account.run_in_background", lambda job: job())
    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", True)

    def load_session(token: str):
        with factory() as session:
            return resolve_active_session(session, token)

    monkeypatch.setattr(app_main, "_load_session_context", load_session)
    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def test_the_admin_refusal_is_generic_over_http(client: TestClient) -> None:
    member = client.post("/auth/phone/request-code", json={"phone": PHONES["buyer"]})
    admin = client.post("/auth/phone/request-code", json={"phone": PHONES["admin"]})
    assert member.status_code == admin.status_code == 202
    assert set(member.json()) == set(admin.json())
    assert admin.json()["resend_after_seconds"] == member.json()["resend_after_seconds"]
    # Even the development echo has nothing to show: no code exists.
    assert admin.json()["dev_code"] is None
    refused = client.post("/auth/phone/verify", json={"phone": PHONES["admin"], "code": "123456"})
    unknown = client.post("/auth/phone/verify", json={"phone": "+254712100055", "code": "123456"})
    assert refused.status_code == unknown.status_code == 410
    assert refused.json() == unknown.json() == {"detail": "That code has expired."}

    reset = client.post("/auth/password/reset/request-code", json={"phone": PHONES["admin"]})
    assert reset.status_code == 202 and reset.json()["dev_code"] is None


def test_a_seller_signs_in_with_a_code_over_http(client: TestClient) -> None:
    code = client.post("/auth/phone/request-code", json={"phone": PHONES["seller"]}).json()[
        "dev_code"
    ]
    signed_in = client.post("/auth/phone/verify", json={"phone": PHONES["seller"], "code": code})
    assert signed_in.status_code == 200
    body = signed_in.json()
    assert body["role"] == "seller" and body["store"]["status"] == "active"
    assert body["sms_login_enabled"] is True


def _sign_in(client: TestClient, who: str) -> None:
    response = client.post("/auth/login", json={"phone": PHONES[who], "password": PASSWORD})
    assert response.status_code == 200
    client.headers[get_settings().csrf_header_name] = response.cookies[
        get_settings().csrf_cookie_name
    ]


def test_members_turn_sms_sign_in_off_and_on(client: TestClient) -> None:
    _sign_in(client, "buyer")
    off = client.put("/account/sms-login", json={"enabled": False})
    assert off.status_code == 200 and off.json() == {"enabled": False}
    assert client.get("/auth/me").json()["sms_login_enabled"] is False
    sent = client.post("/auth/phone/request-code", json={"phone": PHONES["buyer"]})
    assert sent.status_code == 202, sent.text and sent.json()["dev_code"] is None
    assert client.put("/account/sms-login", json={"enabled": True}).json() == {"enabled": True}


def test_admins_have_no_sms_switch(client: TestClient) -> None:
    _sign_in(client, "admin")
    assert client.get("/auth/me").json()["sms_login_enabled"] is False
    assert client.put("/account/sms-login", json={"enabled": True}).status_code == 403


def test_sms_stays_on_while_it_is_the_only_way_in(client: TestClient, factory) -> None:
    # An account made by an SMS code has no known password and no Google link.
    code = client.post("/auth/phone/request-code", json={"phone": "+254712100066"}).json()[
        "dev_code"
    ]
    signed_in = client.post("/auth/phone/verify", json={"phone": "+254712100066", "code": code})
    assert signed_in.json()["has_password"] is False
    client.headers[get_settings().csrf_header_name] = signed_in.cookies[
        get_settings().csrf_cookie_name
    ]
    refused = client.put("/account/sms-login", json={"enabled": False})
    assert refused.status_code == 409
