from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.notification import (
    Notification,
    NotificationDeliveryStatus,
    NotificationEventType,
)
from app.models.transaction import Transaction
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.notification_delivery import install_notification_sms_hooks
from app.services.sms import SmsOutcome, SmsRecipientResult, SmsSendResult
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


class _FakeSender:
    def __init__(self, outcome: SmsOutcome = SmsOutcome.SENT, error: Exception | None = None):
        self.outcome = outcome
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    def send(self, message, recipients):
        self.calls.append((message, list(recipients)))
        if self.error is not None:
            raise self.error
        return SmsSendResult(
            recipients=[
                SmsRecipientResult(phone=phone, outcome=self.outcome, attempts=1)
                for phone in recipients
            ],
            attempts=1,
        )


def _session_factory(sender_provider) -> sessionmaker:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    payload_column = Notification.__table__.c.payload
    original_default = payload_column.server_default
    payload_column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            engine, tables=[User.__table__, Notification.__table__, Transaction.__table__]
        )
    finally:
        payload_column.server_default = original_default

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    install_notification_sms_hooks(factory, sender_provider=sender_provider)
    return factory


def _user(db: Session, role: UserRole = UserRole.BUYER) -> User:
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        name="Bea Buyer",
        phone="+254712345678",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    return user


def _notification(user_id: uuid.UUID) -> Notification:
    return Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        transaction_id=uuid.uuid4(),
        event_type=NotificationEventType.DELIVERY_ARRIVED,
        title="Delivery arrived",
        message="Your seller is at the door.",
        payload={},
    )


def _reload(factory: sessionmaker, notification_id: uuid.UUID) -> Notification:
    with factory() as db:
        return db.execute(
            select(Notification).where(Notification.id == notification_id)
        ).scalar_one()


def test_committed_notification_is_sent_and_marked_sent() -> None:
    sender = _FakeSender()
    factory = _session_factory(lambda: sender)
    with factory() as db:
        user = _user(db)
        notification = _notification(user.id)
        db.add(notification)
        db.commit()

    stored = _reload(factory, notification.id)
    assert stored.delivery_status == NotificationDeliveryStatus.SENT
    assert stored.delivered_at is not None
    assert sender.calls == [
        ("Vekro: Delivery arrived. Your seller is at the door.", ["+254712345678"])
    ]


@pytest.mark.parametrize(
    "sender",
    [
        _FakeSender(outcome=SmsOutcome.FAILED_DEFINITE),
        _FakeSender(outcome=SmsOutcome.FAILED_TRANSIENT),
        _FakeSender(error=RuntimeError("gateway down")),
    ],
)
def test_failed_delivery_is_recorded_without_losing_the_row(sender: _FakeSender) -> None:
    factory = _session_factory(lambda: sender)
    with factory() as db:
        user = _user(db)
        notification = _notification(user.id)
        db.add(notification)
        db.commit()

    stored = _reload(factory, notification.id)
    assert stored.delivery_status == NotificationDeliveryStatus.FAILED
    assert stored.delivered_at is None


def test_missing_sms_credentials_leave_status_not_attempted() -> None:
    factory = _session_factory(lambda: None)
    with factory() as db:
        user = _user(db)
        notification = _notification(user.id)
        db.add(notification)
        db.commit()

    assert _reload(factory, notification.id).delivery_status == (
        NotificationDeliveryStatus.NOT_ATTEMPTED
    )


def test_delivery_infrastructure_error_does_not_break_commit() -> None:
    def _broken_provider():
        raise RuntimeError("settings unavailable")

    factory = _session_factory(_broken_provider)
    with factory() as db:
        user = _user(db)
        notification = _notification(user.id)
        db.add(notification)
        db.commit()

    assert _reload(factory, notification.id).delivery_status == (
        NotificationDeliveryStatus.NOT_ATTEMPTED
    )


def test_rolled_back_notification_is_never_sent() -> None:
    sender = _FakeSender()
    factory = _session_factory(lambda: sender)
    with factory() as db:
        user = _user(db)
        db.add(_notification(user.id))
        db.flush()
        db.rollback()

    assert sender.calls == []


def test_get_notifications_stays_intact_when_sms_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory(lambda: _FakeSender(error=RuntimeError("gateway down")))
    with factory() as db:
        user = _user(db)
        notification = _notification(user.id)
        db.add(notification)
        db.commit()

    settings = get_settings()
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )

    def _override_get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = TestClient(app)
        client.cookies.set(settings.session_cookie_name, "session-token")
        response = client.get("/notifications")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [str(notification.id)]
    assert body[0]["delivery_status"] == "failed"
