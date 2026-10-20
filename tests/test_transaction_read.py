import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.phone import mask_phone
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.listing import Listing
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.deadlines import (
    AT_DOOR_INSPECTION_WINDOW,
    HOLD_WINDOW,
    NO_DISPATCH_WINDOW,
    RETURN_RECEIPT_WINDOW,
    RETURN_SEND_WINDOW,
    deadlines_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _user(role: UserRole, name: str, phone: str) -> User:
    return User(
        id=uuid.uuid4(),
        name=name,
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=phone if role == UserRole.SELLER else None,
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
    column = cast(Any, Listing.__table__.c.dispute_policy)
    original = column.server_default
    column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                cast(Table, User.__table__),
                cast(Table, Listing.__table__),
                cast(Table, Transaction.__table__),
                cast(Table, PaymentPrompt.__table__),
            ],
        )
    finally:
        column.server_default = original
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def parties(db: Session) -> dict[str, Any]:
    buyer = _user(UserRole.BUYER, "Wanjiku Buyer", "+254712345678")
    seller = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    stranger = _user(UserRole.BUYER, "Someone Else", "+254733000111")
    admin = _user(UserRole.ADMIN, "Vekro Support", "+254700000001")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="iPhone 13, 128GB, Midnight",
        price=Decimal("52000.00"),
        is_serialized=True,
        unique_id="352098114371234",
        dispute_policy={"resolution": "repair"},
        created_at=NOW,
        updated_at=NOW,
    )
    transaction = Transaction(
        id=uuid.uuid4(),
        reference="VK-7K3P9Q",
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("52000.00"),
        status=TransactionStatus.LOCKED,
        locked_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add_all([buyer, seller, stranger, admin, listing])
    db.flush()
    db.add(transaction)
    db.commit()
    return {
        "buyer": buyer,
        "seller": seller,
        "stranger": stranger,
        "admin": admin,
        "transaction": transaction,
    }


def _client_as(monkeypatch, db: Session, user: User | None) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    if user is None:
        return client
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
    client.cookies.set(get_settings().session_cookie_name, "session-token")
    return client


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("who", "role"),
    [("buyer", "buyer"), ("seller", "seller"), ("admin", "admin")],
)
def test_parties_and_admins_can_read(monkeypatch, db, parties, who, role) -> None:
    transaction = parties["transaction"]
    client = _client_as(monkeypatch, db, parties[who])

    response = client.get(f"/transactions/{transaction.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(transaction.id)
    assert body["reference"] == "VK-7K3P9Q"
    assert body["status"] == "locked"
    assert body["viewer_role"] == role
    assert body["listing"]["title"] == "iPhone 13, 128GB, Midnight"
    assert body["listing"]["is_serialized"] is True
    assert body["listing"]["dispute_policy"] == {"resolution": "repair"}
    assert body["buyer"] == {"name": "Wanjiku Buyer", "phone_masked": "+254 712 *** 678"}
    assert body["seller"] == {"name": "Amina Gadgets", "phone_masked": "+254 722 *** 904"}
    assert body["deadlines"]["dispatch_by"].startswith("2026-10-22T09:00:00")
    assert body["deadlines"]["inspect_by"] is None
    assert "server_time" in body


def test_other_users_get_not_found(monkeypatch, db, parties) -> None:
    client = _client_as(monkeypatch, db, parties["stranger"])

    response = client.get(f"/transactions/{parties['transaction'].id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found."


def test_missing_transaction_gets_not_found(monkeypatch, db, parties) -> None:
    client = _client_as(monkeypatch, db, parties["buyer"])

    assert client.get(f"/transactions/{uuid.uuid4()}").status_code == 404


def test_requires_a_session(monkeypatch, db, parties) -> None:
    client = _client_as(monkeypatch, db, None)

    assert client.get(f"/transactions/{parties['transaction'].id}").status_code == 401


def test_read_is_in_the_openapi_contract() -> None:
    operation = app.openapi()["paths"]["/transactions/{transaction_id}"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema["$ref"].endswith("/TransactionDetailResponse")


def _transaction(status: TransactionStatus, **stamps: datetime) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount=Decimal("100.00"),
        status=status,
        created_at=NOW,
        updated_at=stamps.pop("updated_at", NOW),
        **stamps,
    )


@pytest.mark.parametrize(
    ("status", "stamps", "field", "window"),
    [
        (TransactionStatus.LOCKED, {"locked_at": NOW}, "dispatch_by", NO_DISPATCH_WINDOW),
        (
            TransactionStatus.AT_DOOR_PENDING_INSPECTION,
            {"at_door_at": NOW},
            "inspect_by",
            AT_DOOR_INSPECTION_WINDOW,
        ),
        (TransactionStatus.HOLD_24H, {"hold_started_at": NOW}, "hold_until", HOLD_WINDOW),
        (
            TransactionStatus.DISPUTED_FUNCTIONAL,
            {"updated_at": NOW},
            "return_send_by",
            RETURN_SEND_WINDOW,
        ),
        (
            TransactionStatus.RETURN_IN_TRANSIT,
            {"updated_at": NOW},
            "return_confirm_by",
            RETURN_RECEIPT_WINDOW,
        ),
    ],
)
def test_deadline_for_each_timed_state(status, stamps, field, window) -> None:
    deadlines = deadlines_for(_transaction(status, **stamps))

    assert getattr(deadlines, field) == NOW + window
    others = {"dispatch_by", "inspect_by", "hold_until", "return_send_by", "return_confirm_by"}
    assert all(getattr(deadlines, name) is None for name in others - {field})


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.AWAITING_PAYMENT,
        TransactionStatus.OUT_FOR_DELIVERY,
        TransactionStatus.RELEASED,
        TransactionStatus.RESOLVED_REFUND,
    ],
)
def test_untimed_states_have_no_deadline(status) -> None:
    deadlines = deadlines_for(_transaction(status, locked_at=NOW))

    assert deadlines == type(deadlines)()


def test_naive_timestamps_are_treated_as_utc() -> None:
    deadlines = deadlines_for(
        _transaction(TransactionStatus.LOCKED, locked_at=NOW.replace(tzinfo=None))
    )

    assert deadlines.dispatch_by == NOW + NO_DISPATCH_WINDOW


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("+254712345678", "+254 712 *** 678"),
        ("+447700900123", "+44 *** 123"),
        (None, None),
        ("123", "***"),
    ],
)
def test_mask_phone(raw, masked) -> None:
    assert mask_phone(raw) == masked
