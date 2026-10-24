import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.notification import Notification, NotificationChannel
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.orm import Session, sessionmaker

from tests.test_notification_sms_delivery import _notification, _session_factory, _user


def _other(db: Session) -> User:
    """A second user; the shared helper always uses the same phone."""
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        name="Other User",
        phone="+254700999888",
        role=UserRole.BUYER,
        password_hash="unused",
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def factory() -> sessionmaker:
    made = _session_factory(lambda: None)
    Base.metadata.create_all(made.kw["bind"], tables=[cast(Table, Transaction.__table__)])
    return made


@pytest.fixture
def db(factory: sessionmaker) -> Iterator[Session]:
    with factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _client_as(monkeypatch, db: Session, user: User) -> TestClient:
    settings = get_settings()
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set(settings.session_cookie_name, "session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    client.headers[settings.csrf_header_name] = "csrf-token"
    return client


def _add(db: Session, user: User, *, channel=NotificationChannel.IN_APP, minutes=0, read=False):
    notification = _notification(user.id)
    notification.channel = channel
    notification.created_at = datetime.now(UTC) - timedelta(minutes=minutes)
    if read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC) - timedelta(days=1)
    db.add(notification)
    db.commit()
    return notification


def test_unread_count_counts_only_my_unread_in_app(monkeypatch, db) -> None:
    me, other = _user(db), _other(db)
    _add(db, me)
    _add(db, me, minutes=1)
    _add(db, me, channel=NotificationChannel.SMS)
    _add(db, me, read=True)
    _add(db, other)
    client = _client_as(monkeypatch, db, me)

    assert client.get("/notifications/unread-count").json() == {"unread_count": 2}


def test_list_filters_unread_and_channel(monkeypatch, db) -> None:
    me = _user(db)
    unread = _add(db, me)
    _add(db, me, read=True, minutes=1)
    _add(db, me, channel=NotificationChannel.SMS, minutes=2)
    client = _client_as(monkeypatch, db, me)

    body = client.get("/notifications", params={"unread": True, "channel": "in_app"}).json()

    assert [item["id"] for item in body] == [str(unread.id)]


def test_mark_one_read_sets_read_at_once(monkeypatch, db) -> None:
    me = _user(db)
    notification = _add(db, me)
    client = _client_as(monkeypatch, db, me)

    first = client.post(f"/notifications/{notification.id}/read")
    second = client.post(f"/notifications/{notification.id}/read")

    assert first.status_code == 200
    assert first.json()["is_read"] is True
    assert first.json()["read_at"] is not None
    assert second.json()["read_at"] == first.json()["read_at"]


def test_cannot_mark_someone_elses_notification(monkeypatch, db) -> None:
    me, other = _user(db), _other(db)
    theirs = _add(db, other)
    client = _client_as(monkeypatch, db, me)

    response = client.post(f"/notifications/{theirs.id}/read")

    assert response.status_code == 404
    db.refresh(theirs)
    assert theirs.is_read is False


def test_missing_notification_is_not_found(monkeypatch, db) -> None:
    client = _client_as(monkeypatch, db, _user(db))

    assert client.post(f"/notifications/{uuid.uuid4()}/read").status_code == 404


def test_read_all_marks_only_mine_and_keeps_old_read_at(monkeypatch, db) -> None:
    me, other = _user(db), _other(db)
    _add(db, me)
    _add(db, me, channel=NotificationChannel.SMS)
    already = _add(db, me, read=True)
    old_read_at = already.read_at
    theirs = _add(db, other)
    client = _client_as(monkeypatch, db, me)

    response = client.post("/notifications/read-all")

    assert response.json() == {"updated": 2, "unread_count": 0}
    assert client.get("/notifications/unread-count").json() == {"unread_count": 0}
    db.expire_all()
    assert db.get(Notification, theirs.id).is_read is False
    # SQLite drops the timezone on reload.
    assert db.get(Notification, already.id).read_at.replace(tzinfo=None) == old_read_at.replace(
        tzinfo=None
    )


def test_marking_read_needs_the_csrf_header(monkeypatch, db) -> None:
    me = _user(db)
    notification = _add(db, me)
    client = _client_as(monkeypatch, db, me)
    del client.headers[get_settings().csrf_header_name]

    assert client.post(f"/notifications/{notification.id}/read").status_code == 403
    assert client.post("/notifications/read-all").status_code == 403


def test_read_endpoints_need_a_session() -> None:
    client = TestClient(app)

    assert client.get("/notifications/unread-count").status_code == 401
    assert client.post("/notifications/read-all").status_code in {401, 403}


def test_notifications_carry_the_order_reference(monkeypatch, db) -> None:
    me = _user(db)
    notification = _add(db, me)
    db.add(
        Transaction(
            id=notification.transaction_id,
            reference="VK-7K3P9Q",
            listing_id=uuid.uuid4(),
            buyer_id=me.id,
            seller_id=uuid.uuid4(),
            amount=Decimal("52000.00"),
            status=TransactionStatus.LOCKED,
        )
    )
    db.commit()
    client = _client_as(monkeypatch, db, me)

    listed = client.get("/notifications").json()
    marked = client.post(f"/notifications/{notification.id}/read").json()

    assert listed[0]["transaction_reference"] == "VK-7K3P9Q"
    assert marked["transaction_reference"] == "VK-7K3P9Q"
