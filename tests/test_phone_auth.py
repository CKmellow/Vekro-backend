from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.phone_code import PhoneCode
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services import phone_auth
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneCodeRateLimitedError,
    PhoneNotBuyerError,
    request_phone_code,
    verify_phone_code,
)
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

PHONE = "+254712000111"
T0 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def send(self, message: str, recipients: Any) -> Any:
        self.sent.append((message, list(recipients)))
        return None


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
    return factory()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    phone_auth._CLIENT_BUCKETS.clear()
    # Never reach the real SMS gateway from tests.
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)


def _request(db: Session, at: datetime = T0, sender: FakeSender | None = None, **kwargs: Any):
    return request_phone_code(
        db, PHONE, now=at, sender_provider=lambda: sender, **kwargs  # type: ignore[arg-type,return-value]
    )


def test_sends_a_six_digit_code_and_stores_only_its_hash(db: Session) -> None:
    sender = FakeSender()
    issued = _request(db, sender=sender)

    assert len(issued.code) == 6 and issued.code.isdigit()
    assert issued.expires_at == T0 + timedelta(minutes=5)
    assert issued.resend_after_seconds == 30
    row = db.execute(select(PhoneCode)).scalar_one()
    assert issued.code not in row.code_hash
    assert sender.sent == [(phone_auth.sign_in_sms_text(issued.code, 300), [PHONE])]
    assert "Never share it" in sender.sent[0][0]


def test_resend_waits_for_the_cooldown_then_replaces_the_old_code(db: Session) -> None:
    first = _request(db)
    with pytest.raises(PhoneCodeRateLimitedError) as raised:
        _request(db, T0 + timedelta(seconds=10))
    assert raised.value.retry_after_seconds == 20

    second = _request(db, T0 + timedelta(seconds=31))
    if first.code != second.code:
        with pytest.raises(PhoneCodeInvalidError):
            verify_phone_code(db, PHONE, first.code, now=T0 + timedelta(seconds=32))
    session = verify_phone_code(db, PHONE, second.code, now=T0 + timedelta(seconds=33))
    assert session.user.phone == PHONE


def test_caps_codes_per_phone_per_hour(db: Session) -> None:
    for i in range(5):
        _request(db, T0 + timedelta(minutes=i))
    with pytest.raises(PhoneCodeRateLimitedError) as raised:
        _request(db, T0 + timedelta(minutes=5))
    assert raised.value.retry_after_seconds == 55 * 60
    _request(db, T0 + timedelta(minutes=61))


def test_caps_codes_per_client_across_numbers(db: Session) -> None:
    for i in range(phone_auth.CLIENT_HOURLY_LIMIT):
        request_phone_code(db, f"+2547120{i:05d}", client_identifier="1.2.3.4", now=T0)
    with pytest.raises(PhoneCodeRateLimitedError):
        request_phone_code(db, "+254799999999", client_identifier="1.2.3.4", now=T0)
    request_phone_code(db, "+254799999999", client_identifier="5.6.7.8", now=T0)


def test_verifying_creates_a_buyer_and_opens_a_session(db: Session) -> None:
    issued = _request(db)
    session = verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(minutes=1))

    user = db.execute(select(User).where(User.phone == PHONE)).scalar_one()
    assert user.role == UserRole.BUYER
    assert session.user.id == user.id
    assert db.execute(select(UserSession)).scalar_one().user_id == user.id
    # Single use.
    with pytest.raises(PhoneCodeExpiredError):
        verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(minutes=2))


def test_verifying_signs_in_an_existing_buyer(db: Session) -> None:
    db.add(User(name="Wanjiru", phone=PHONE, role=UserRole.BUYER, password_hash="x"))
    db.commit()
    issued = _request(db)
    session = verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(minutes=1))
    assert session.user.name == "Wanjiru"
    assert len(db.execute(select(User)).scalars().all()) == 1


def test_sellers_must_use_their_password(db: Session) -> None:
    db.add(User(name="Amina", phone=PHONE, role=UserRole.SELLER, password_hash="x"))
    db.commit()
    issued = _request(db)
    with pytest.raises(PhoneNotBuyerError):
        verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(minutes=1))
    assert db.execute(select(UserSession)).first() is None


def test_expired_codes_are_refused(db: Session) -> None:
    issued = _request(db)
    with pytest.raises(PhoneCodeExpiredError):
        verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(minutes=5, seconds=1))


def test_a_code_is_bound_to_its_phone(db: Session) -> None:
    issued = _request(db)
    request_phone_code(db, "+254712000222", now=T0)
    with pytest.raises(PhoneCodeInvalidError):
        verify_phone_code(db, "+254712000222", issued.code, now=T0 + timedelta(seconds=5))


def test_wrong_codes_count_down_then_lock(db: Session) -> None:
    issued = _request(db)
    wrong = "000000" if issued.code != "000000" else "111111"
    for left in (4, 3, 2, 1):
        with pytest.raises(PhoneCodeInvalidError) as raised:
            verify_phone_code(db, PHONE, wrong, now=T0 + timedelta(seconds=5))
        assert raised.value.attempts_left == left
    with pytest.raises(PhoneCodeRateLimitedError) as locked:
        verify_phone_code(db, PHONE, wrong, now=T0 + timedelta(seconds=6))
    assert locked.value.retry_after_seconds == 300

    # The right code no longer works, and no new code until the lockout passes.
    with pytest.raises(PhoneCodeRateLimitedError):
        verify_phone_code(db, PHONE, issued.code, now=T0 + timedelta(seconds=7))
    with pytest.raises(PhoneCodeRateLimitedError):
        _request(db, T0 + timedelta(minutes=2))
    _request(db, T0 + timedelta(minutes=6))


@pytest.fixture
def client(factory: sessionmaker, monkeypatch: pytest.MonkeyPatch):
    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("app.routers.auth.run_in_background", lambda job: job())
    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _echo(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", on)


def test_endpoints_sign_a_buyer_in_with_session_cookies(client, monkeypatch) -> None:
    _echo(monkeypatch, True)
    sent = client.post("/auth/phone/request-code", json={"phone": "0712000111"})
    assert sent.status_code == 202
    body = sent.json()
    assert body["resend_after_seconds"] == 30
    assert body["dev_code"] and len(body["dev_code"]) == 6

    verified = client.post(
        "/auth/phone/verify", json={"phone": "+254712000111", "code": body["dev_code"]}
    )
    assert verified.status_code == 200
    assert verified.json()["role"] == "buyer"
    settings = get_settings()
    assert verified.cookies.get(settings.session_cookie_name)
    assert verified.cookies.get(settings.csrf_cookie_name)


def test_endpoints_never_echo_the_code_by_default(client, monkeypatch) -> None:
    _echo(monkeypatch, False)
    sent = client.post("/auth/phone/request-code", json={"phone": PHONE})
    assert sent.status_code == 202
    assert sent.json()["dev_code"] is None


def test_endpoint_errors(client, monkeypatch, factory) -> None:
    _echo(monkeypatch, True)
    assert (
        client.post("/auth/phone/verify", json={"phone": PHONE, "code": "123456"}).status_code
        == 410
    )

    code = client.post("/auth/phone/request-code", json={"phone": PHONE}).json()["dev_code"]
    again = client.post("/auth/phone/request-code", json={"phone": PHONE})
    assert again.status_code == 429 and int(again.headers["Retry-After"]) > 0

    wrong = "000000" if code != "000000" else "111111"
    bad = client.post("/auth/phone/verify", json={"phone": PHONE, "code": wrong})
    assert bad.status_code == 422 and bad.json()["detail"] == "That code doesn't match."
    for _ in range(3):
        client.post("/auth/phone/verify", json={"phone": PHONE, "code": wrong})
    locked = client.post("/auth/phone/verify", json={"phone": PHONE, "code": wrong})
    assert locked.status_code == 429 and locked.headers["Retry-After"] == "300"

    malformed = client.post("/auth/phone/verify", json={"phone": PHONE, "code": "12ab"})
    assert malformed.status_code == 422

    with factory() as session:
        session.add(
            User(name="Amina", phone="+254712000333", role=UserRole.SELLER, password_hash="x")
        )
        session.commit()
    seller_code = client.post("/auth/phone/request-code", json={"phone": "+254712000333"}).json()[
        "dev_code"
    ]
    seller = client.post("/auth/phone/verify", json={"phone": "+254712000333", "code": seller_code})
    assert seller.status_code == 409
