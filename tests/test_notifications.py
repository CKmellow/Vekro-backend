import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.notification import Notification, NotificationChannel, NotificationEventType
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import notifications as notifications_router
from app.services.auth import ActiveSessionContext
from app.services.notification import list_user_notifications
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeNotificationResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = None

    def execute(self, query):
        self.last_query = query
        return _FakeNotificationResult(self._rows)


def _build_user(role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} User",
        phone="+254700123456",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_session(user_id: uuid.UUID) -> UserSession:
    settings = get_settings()
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_notification(
    *,
    user_id: uuid.UUID,
    created_at: datetime,
    title: str,
    event_type: NotificationEventType = NotificationEventType.DISPUTE_UPDATED,
) -> Notification:
    return Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        transaction_id=uuid.uuid4(),
        event_type=event_type,
        channel=NotificationChannel.IN_APP,
        title=title,
        message=f"{title} message",
        payload={"title": title},
        is_read=False,
        read_at=None,
        delivered_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _authenticated_client(monkeypatch, role: UserRole) -> tuple[TestClient, User]:
    user = _build_user(role)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "session-token")
    return client, user


def test_list_notifications_requires_authentication() -> None:
    client = TestClient(app)
    response = client.get("/notifications")

    assert response.status_code == 401


def test_list_notifications_returns_current_user_only(monkeypatch) -> None:
    client, auth_user = _authenticated_client(monkeypatch, UserRole.BUYER)
    now = datetime.now(UTC)
    own_notification = _build_notification(
        user_id=auth_user.id,
        created_at=now,
        title="Own notification",
    )

    monkeypatch.setattr(
        notifications_router,
        "list_user_notifications",
        lambda _db, user_id, limit, offset, **_filters: [own_notification],
    )
    monkeypatch.setattr(notifications_router, "transaction_references", lambda _db, _ids: {})

    response = client.get("/notifications")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["user_id"] == str(auth_user.id)
    assert body[0]["title"] == "Own notification"


def test_list_notifications_forwards_pagination_params(monkeypatch) -> None:
    client, auth_user = _authenticated_client(monkeypatch, UserRole.BUYER)
    observed: dict[str, object] = {}

    def fake_list_user_notifications(_db, user_id, limit, offset, unread_only, channel):
        observed["user_id"] = user_id
        observed["limit"] = limit
        observed["offset"] = offset
        observed["unread_only"] = unread_only
        observed["channel"] = channel
        return []

    monkeypatch.setattr(
        notifications_router,
        "list_user_notifications",
        fake_list_user_notifications,
    )
    monkeypatch.setattr(notifications_router, "transaction_references", lambda _db, _ids: {})

    response = client.get("/notifications?limit=5&offset=10&unread=true&channel=in_app")

    assert response.status_code == 200
    assert observed["user_id"] == auth_user.id
    assert observed["limit"] == 5
    assert observed["offset"] == 10
    assert observed["unread_only"] is True
    assert observed["channel"] == NotificationChannel.IN_APP


def test_service_list_user_notifications_filters_and_orders_desc() -> None:
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    now = datetime.now(UTC)

    oldest = _build_notification(
        user_id=owner_id,
        created_at=now - timedelta(minutes=2),
        title="oldest",
    )
    newest_other_user = _build_notification(
        user_id=other_id,
        created_at=now,
        title="other user",
    )
    newest_owner = _build_notification(
        user_id=owner_id,
        created_at=now - timedelta(minutes=1),
        title="newest owner",
    )

    db = _FakeDb(rows=[oldest, newest_other_user, newest_owner])

    notifications = list_user_notifications(
        cast(Session, db),
        user_id=owner_id,
        limit=50,
        offset=0,
    )

    assert [item.title for item in notifications] == ["newest owner", "oldest"]
    assert all(item.user_id == owner_id for item in notifications)

    query_text = str(db.last_query)
    assert "notifications.user_id" in query_text
    assert "ORDER BY notifications.created_at DESC" in query_text


def test_service_pagination_is_scoped_to_owner_before_limit() -> None:
    from tests.test_notification_sms_delivery import _notification, _session_factory, _user

    factory = _session_factory(lambda: None)
    with factory() as db:
        owner = _user(db)
        other = User(
            id=uuid.uuid4(),
            name="Other User",
            phone="+254700999888",
            role=UserRole.BUYER,
            password_hash="unused",
            session_version=1,
            is_active=True,
            failed_login_attempts=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(other)
        own = _notification(owner.id)
        own.created_at = datetime.now(UTC) - timedelta(minutes=5)
        newer_foreign = _notification(other.id)
        newer_foreign.created_at = datetime.now(UTC)
        db.add_all([own, newer_foreign])
        db.commit()

        page = list_user_notifications(db, user_id=owner.id, limit=1, offset=0)

    assert [item.id for item in page] == [own.id]
