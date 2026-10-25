import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from app.core.security import hash_password, verify_password
from app.db.base import Base
from app.models.notification import Notification, NotificationEventType
from app.models.phone_code import PhoneCode
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services import phone_auth
from app.services.account_security import (
    CurrentSessionError,
    SessionNotFoundError,
    WrongPasswordError,
    change_password,
    device_label,
    list_sessions,
    request_reset_code,
    reset_password,
    revoke_other_sessions,
    revoke_session,
)
from app.services.auth import create_session
from app.services.notification_prefs import muted_event_types, preferences, set_preferences
from app.services.phone_auth import PhoneCodeExpiredError, PhoneCodeInvalidError
from sqlalchemy import Table, create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


PHONE = "+254722118904"
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/129.0 Mobile Safari/537.36"
)


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [User.__table__, UserSession.__table__, PhoneCode.__table__]
    Base.metadata.create_all(engine, tables=[cast(Table, t) for t in tables])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    phone_auth._CLIENT_BUCKETS.clear()
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)


@pytest.fixture
def seller(db: Session) -> User:
    user = User(
        name="Amina", phone=PHONE, role=UserRole.SELLER, password_hash=hash_password("Mango2026")
    )
    db.add(user)
    db.commit()
    return user


def _sessions(db: Session, user: User, n: int) -> list[uuid.UUID]:
    out = []
    for i in range(n):
        create_session(
            db, user, datetime.now(UTC) - timedelta(minutes=i), user_agent=CHROME_ANDROID
        )
    for row in db.execute(select(UserSession).order_by(UserSession.created_at.desc())).scalars():
        out.append(row.id)
    return out


def test_device_labels() -> None:
    assert device_label(CHROME_ANDROID) == "Chrome on Android"
    assert device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Version/17.0 Safari/604.1") == (
        "Safari on iPhone"
    )
    assert device_label(None) == "Unknown device"


def test_change_password_signs_out_other_devices(db: Session, seller: User) -> None:
    current, *others = _sessions(db, seller, 3)
    with pytest.raises(WrongPasswordError):
        change_password(
            db, seller, current_password="nope", new_password="Papaya2027", keep_session=current
        )
    assert (
        change_password(
            db,
            seller,
            current_password="Mango2026",
            new_password="Papaya2027",
            keep_session=current,
        )
        == 2
    )
    assert verify_password("Papaya2027", seller.password_hash)
    assert [s.id for s in list_sessions(db, seller.id, current)] == [current]


def test_sessions_list_and_revoke(db: Session, seller: User) -> None:
    current, other, third = _sessions(db, seller, 3)
    listed = list_sessions(db, seller.id, current)
    assert listed[0].current and listed[0].device == "Chrome on Android"
    with pytest.raises(CurrentSessionError):
        revoke_session(db, seller.id, current, current)
    with pytest.raises(SessionNotFoundError):
        revoke_session(db, uuid.uuid4(), other, current)
    revoke_session(db, seller.id, other, current)
    assert {s.id for s in list_sessions(db, seller.id, current)} == {current, third}
    assert revoke_other_sessions(db, seller.id, current) == 1


def test_reset_password_by_sms(db: Session, seller: User) -> None:
    current, _ = _sessions(db, seller, 2)
    assert request_reset_code(db, "+254700000000", client_identifier="x") is None
    issued = request_reset_code(db, PHONE, client_identifier="x")
    assert issued is not None
    wrong = "000000" if issued.code != "000000" else "111111"
    with pytest.raises(PhoneCodeInvalidError):
        reset_password(db, PHONE, wrong, "Papaya2027")
    reset_password(db, PHONE, issued.code, "Papaya2027")
    assert verify_password("Papaya2027", seller.password_hash)
    assert list_sessions(db, seller.id, current) == []
    with pytest.raises(PhoneCodeExpiredError):
        reset_password(db, PHONE, issued.code, "Other2028")


def test_a_sign_in_code_cannot_reset_a_password(db: Session, seller: User) -> None:
    code = phone_auth.request_phone_code(db, PHONE, purpose="sign_in").code
    with pytest.raises(PhoneCodeExpiredError):
        reset_password(db, PHONE, code, "Papaya2027")


def test_preferences_default_on_and_support_in_app_locked(seller: User) -> None:
    assert preferences(seller)["returns"] == {"in_app": True, "sms": True}
    set_preferences(seller, {"returns": {"sms": False}, "support": {"in_app": False, "sms": False}})
    prefs = preferences(seller)
    assert prefs["returns"] == {"in_app": True, "sms": False}
    assert prefs["support"] == {"in_app": True, "sms": False}
    assert muted_event_types(seller, "sms") >= {
        NotificationEventType.DISPUTE_OPENED,
        NotificationEventType.ADMIN_DECISION,
    }
    assert NotificationEventType.ADMIN_DECISION not in muted_event_types(seller, "in_app")


def test_buyers_can_never_be_muted() -> None:
    buyer = User(name="B", phone=PHONE, role=UserRole.BUYER, password_hash="x")
    buyer.notification_prefs = {"delivery": {"in_app": False, "sms": False}}
    assert muted_event_types(buyer, "sms") == set()


def test_sms_delivery_skips_muted_categories(db: Session, seller: User) -> None:
    from app.services.notification_delivery import deliver_notification_sms

    engine = db.get_bind()
    payload = cast(Table, Notification.__table__).c.payload
    original = payload.server_default
    payload.server_default = text("'{}'")  # type: ignore[assignment]
    try:
        Base.metadata.create_all(engine, tables=[cast(Table, Notification.__table__)])
    finally:
        payload.server_default = original
    set_preferences(seller, {"returns": {"sms": False}})
    db.commit()
    muted = Notification(
        id=uuid.uuid4(),
        user_id=seller.id,
        transaction_id=uuid.uuid4(),
        event_type=NotificationEventType.DISPUTE_OPENED,
        title="Return opened",
        message="m",
    )
    sent_ok = Notification(
        id=uuid.uuid4(),
        user_id=seller.id,
        transaction_id=uuid.uuid4(),
        event_type=NotificationEventType.TRANSACTION_LOCKED,
        title="Paid",
        message="m",
    )
    db.add_all([muted, sent_ok])
    db.commit()

    class Sender:
        calls: list[str] = []

        def send(self, message, recipients):
            from app.services.sms import SmsSendResult

            self.calls.append(message)
            return SmsSendResult()

    sender = Sender()
    result = deliver_notification_sms(db, [muted.id, sent_ok.id], sender=sender)
    assert result.skipped == 1
    assert len(sender.calls) == 1 and "Paid" in sender.calls[0]


@pytest.fixture
def client(db: Session, seller: User, monkeypatch: pytest.MonkeyPatch):
    from app.core.auth_context import require_seller_user
    from app.db.session import get_db
    from app.main import app
    from app.routers import account
    from fastapi.testclient import TestClient

    monkeypatch.setattr(account, "run_in_background", lambda job: job())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_seller_user] = lambda: seller
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_reset_request_never_reveals_accounts(client, monkeypatch) -> None:
    from app.core.settings import get_settings

    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", True)
    unknown = client.post("/auth/password/reset/request-code", json={"phone": "0700000000"})
    known = client.post("/auth/password/reset/request-code", json={"phone": "0722118904"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json()["dev_code"] is None and len(known.json()["dev_code"]) == 6
    weak = client.post(
        "/auth/password/reset",
        json={"phone": PHONE, "code": known.json()["dev_code"], "new_password": "password"},
    )
    assert weak.status_code == 422
    done = client.post(
        "/auth/password/reset",
        json={"phone": PHONE, "code": known.json()["dev_code"], "new_password": "Papaya2027"},
    )
    assert done.status_code == 204


def test_preferences_endpoints(client) -> None:
    got = client.get("/me/notification-preferences").json()
    assert got["support"] == {"in_app": True, "sms": True}
    body = {
        **got,
        "returns": {"in_app": True, "sms": False},
        "support": {"in_app": False, "sms": True},
    }
    saved = client.put("/me/notification-preferences", json=body).json()
    assert saved["returns"] == {"in_app": True, "sms": False}
    assert saved["support"]["in_app"] is True
