import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select, text  # noqa: F401
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.sqlite_tables import create_disputes_table


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _user(role: UserRole, phone: str, mpesa_phone: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value} user",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=mpesa_phone,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    json_columns = [
        cast(Any, Listing.__table__.c.dispute_policy),
        cast(Any, Notification.__table__.c.payload),
    ]
    originals = {column: column.server_default for column in json_columns}
    for column in json_columns:
        column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                cast(Table, User.__table__),
                cast(Table, Listing.__table__),
                cast(Table, Transaction.__table__),
                cast(Table, Notification.__table__),
                cast(Table, PaymentPrompt.__table__),
            ],
        )
    finally:
        for column, default in originals.items():
            column.server_default = default
    create_disputes_table(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def world(db: Session) -> dict[str, Any]:
    buyer = _user(UserRole.BUYER, "+254712345678")
    other_buyer = _user(UserRole.BUYER, "+254733000111")
    seller = _user(UserRole.SELLER, "+254722118904", "+254722118904")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Nike Air Force 1, size 42",
        price=Decimal("6500.00"),
        is_serialized=False,
        unique_id=None,
        dispute_policy={},
        created_at=NOW,
        updated_at=NOW,
    )
    db.add_all([buyer, other_buyer, seller, listing])
    db.commit()
    return {"buyer": buyer, "other_buyer": other_buyer, "seller": seller, "listing": listing}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _client_as(monkeypatch, db: Session, user: User) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=hash_session_token("session-token", get_settings().secret_key),
        csrf_token_hash=hash_session_token("csrf-token", get_settings().secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )
    client = TestClient(app)
    client.cookies.set(get_settings().session_cookie_name, "session-token")
    client.cookies.set(get_settings().csrf_cookie_name, "csrf-token")
    client.headers[get_settings().csrf_header_name] = "csrf-token"
    return client


def _walk_to_the_door(monkeypatch, db, world) -> tuple[str, str]:
    buyer = _client_as(monkeypatch, db, world["buyer"])
    created = buyer.post("/transactions", json={"listing_id": str(world["listing"].id)})
    assert created.status_code == 201
    order_id = created.json()["id"]
    buyer.post(
        "/transactions/payment-callback",
        json={"transaction_id": order_id, "result_code": 0},
    )
    seller = _client_as(monkeypatch, db, world["seller"])
    assert seller.post(f"/transactions/{order_id}/dispatch").status_code == 200
    assert seller.post(f"/transactions/{order_id}/arrival").status_code == 200
    code = next(
        n.payload["delivery_code"]
        for n in db.execute(select(Notification)).scalars()
        if n.user_id == world["buyer"].id and "delivery_code" in (n.payload or {})
    )
    return order_id, code


def test_buyer_timeline_tells_the_story_without_the_code(monkeypatch, db, world) -> None:
    order_id, code = _walk_to_the_door(monkeypatch, db, world)
    buyer = _client_as(monkeypatch, db, world["buyer"])

    response = buyer.get(f"/transactions/{order_id}/timeline")

    assert response.status_code == 200
    events = response.json()
    assert [e["kind"] for e in events] == [
        "transaction_created",
        "transaction_locked",
        "transaction_dispatched",
        "delivery_arrived",
    ]
    assert [e["title"] for e in events] == [
        "Order created",
        "Paid into the vault",
        "On the way",
        "Arrived at the door",
    ]
    assert events[3]["description"].startswith("Your delivery code was sent by SMS.")
    assert code not in response.text
    assert set(events[0]) == {"kind", "at", "title", "description"}


def test_seller_timeline_uses_seller_words(monkeypatch, db, world) -> None:
    order_id, code = _walk_to_the_door(monkeypatch, db, world)
    seller = _client_as(monkeypatch, db, world["seller"])

    response = seller.get(f"/transactions/{order_id}/timeline")

    events = response.json()
    assert events[2]["description"] == "You sent the item."
    assert events[3]["description"] == "The buyer is inspecting the item."
    assert code not in response.text


def test_admin_sees_each_event_once(monkeypatch, db, world) -> None:
    order_id, code = _walk_to_the_door(monkeypatch, db, world)
    admin_user = _user(UserRole.ADMIN, "+254700000001")
    db.add(admin_user)
    db.commit()
    admin = _client_as(monkeypatch, db, admin_user)

    response = admin.get(f"/transactions/{order_id}/timeline")

    assert len(response.json()) == 4
    assert code not in response.text


def test_strangers_get_not_found(monkeypatch, db, world) -> None:
    order_id, _ = _walk_to_the_door(monkeypatch, db, world)
    other = _client_as(monkeypatch, db, world["other_buyer"])

    assert other.get(f"/transactions/{order_id}/timeline").status_code == 404
    assert other.get(f"/transactions/{uuid.uuid4()}/timeline").status_code == 404


def test_unknown_titles_fall_back_to_safe_copy() -> None:
    from app.models.notification import NotificationEventType
    from app.services.transaction_timeline import _event

    notification = Notification(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        event_type=NotificationEventType.SYSTEM_TIMEOUT,
        title="Something new",
        message="secret 123456",
        payload={"delivery_code": "123456"},
        created_at=NOW,
    )
    event = _event(notification, UserRole.BUYER)

    assert event.title == "Deadline passed"
    assert "123456" not in event.title + event.description
