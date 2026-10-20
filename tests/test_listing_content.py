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
from app.models.listing_photo import ListingPhoto
from app.models.notification import Notification
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.listing_content import MAX_PHOTO_BYTES, listing_status
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
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


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
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
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
                cast(Table, ListingPhoto.__table__),
                cast(Table, Transaction.__table__),
                cast(Table, Notification.__table__),
                cast(Table, PaymentPrompt.__table__),
            ],
        )
    finally:
        for column, default in originals.items():
            column.server_default = default
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def people(db: Session) -> dict[str, User]:
    users = {
        "seller": _user(UserRole.SELLER, "Amina Gadgets", "+254722118904"),
        "other_seller": _user(UserRole.SELLER, "Other Shop", "+254722000999"),
        "buyer": _user(UserRole.BUYER, "Wanjiku", "+254712345678"),
    }
    db.add_all(users.values())
    db.commit()
    return users


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _client(monkeypatch, db: Session, user: User | None = None) -> TestClient:
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
    client.cookies.set(get_settings().csrf_cookie_name, "csrf-token")
    client.headers[get_settings().csrf_header_name] = "csrf-token"
    return client


def _create_listing(client: TestClient, **extra: Any) -> dict[str, Any]:
    response = client.post(
        "/listings",
        json={"title": "Nike Air Force 1, size 42", "price": "6500.00", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, listing_id: str, data: bytes = JPEG, kind: str = "image/jpeg"):
    return client.post(
        f"/listings/{listing_id}/photos", content=data, headers={"Content-Type": kind}
    )


def _add_transaction(db: Session, listing_id: str, people, status: TransactionStatus) -> None:
    db.add(
        Transaction(
            id=uuid.uuid4(),
            listing_id=uuid.UUID(listing_id),
            buyer_id=people["buyer"].id,
            seller_id=people["seller"].id,
            amount=Decimal("6500.00"),
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.commit()


def test_create_and_read_listing_content(monkeypatch, db, people) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    created = _create_listing(
        seller,
        description="  Worn twice, original box.  ",
        category="Shoes",
        buyer_name=" Wanjiku ",
    )

    body = _client(monkeypatch, db).get(f"/listings/{created['id']}").json()

    assert body["description"] == "Worn twice, original box."
    assert body["category"] == "Shoes"
    assert body["buyer_name"] == "Wanjiku"
    assert body["status"] == "active"
    assert body["photos"] == []
    assert body["seller"] == {
        "name": "Amina Gadgets",
        "member_since": "2026-01-05T00:00:00Z",
        "completed_sales": 0,
    }


def test_content_fields_are_optional(monkeypatch, db, people) -> None:
    body = _create_listing(_client(monkeypatch, db, people["seller"]))

    assert body["description"] == ""
    assert body["category"] is None
    assert body["buyer_name"] is None


def test_photos_upload_in_order_and_serve_publicly(monkeypatch, db, people) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    listing = _create_listing(seller)
    first = _upload(seller, listing["id"], JPEG + b"1")
    second = _upload(seller, listing["id"], b"\x89PNG" + b"2", "image/png")

    assert first.status_code == 201 and second.status_code == 201
    photos = _client(monkeypatch, db).get(f"/listings/{listing['id']}").json()["photos"]
    assert [photo["id"] for photo in photos] == [first.json()["id"], second.json()["id"]]

    image = _client(monkeypatch, db).get(photos[1]["url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert "immutable" in image.headers["cache-control"]
    assert image.content == b"\x89PNG" + b"2"


def test_at_most_four_photos(monkeypatch, db, people) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    listing = _create_listing(seller)
    for _ in range(4):
        assert _upload(seller, listing["id"]).status_code == 201

    assert _upload(seller, listing["id"]).status_code == 409


@pytest.mark.parametrize(
    ("data", "kind", "expected"),
    [
        (JPEG, "image/gif", 415),
        (b"", "image/jpeg", 422),  # FastAPI rejects a missing body first
        (b"0" * (MAX_PHOTO_BYTES + 1), "image/jpeg", 413),
        (JPEG, "image/webp; charset=binary", 201),
    ],
)
def test_photo_type_and_size_checks(monkeypatch, db, people, data, kind, expected) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    listing = _create_listing(seller)

    assert _upload(seller, listing["id"], data, kind).status_code == expected


def test_only_the_owner_adds_photos(monkeypatch, db, people) -> None:
    listing = _create_listing(_client(monkeypatch, db, people["seller"]))

    other = _client(monkeypatch, db, people["other_seller"])
    assert _upload(other, listing["id"]).status_code == 403
    assert _upload(other, str(uuid.uuid4())).status_code == 404
    buyer = _client(monkeypatch, db, people["buyer"])
    assert _upload(buyer, listing["id"]).status_code == 403


def test_photo_must_belong_to_the_listing(monkeypatch, db, people) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    first = _create_listing(seller)
    second = _create_listing(seller)
    photo = _upload(seller, first["id"]).json()

    public = _client(monkeypatch, db)
    assert public.get(f"/listings/{second['id']}/photos/{photo['id']}").status_code == 404
    assert public.get(f"/listings/{first['id']}/photos/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "active"),
        ([TransactionStatus.AWAITING_PAYMENT], "awaiting_payment"),
        ([TransactionStatus.LOCKED], "sold"),
        ([TransactionStatus.HOLD_24H], "sold"),
        ([TransactionStatus.RELEASED], "sold"),
        ([TransactionStatus.DISPUTED_FUNCTIONAL], "sold"),
        ([TransactionStatus.REFUNDED_BUYER], "active"),
        (
            [TransactionStatus.RETURNED_TO_SELLER, TransactionStatus.AWAITING_PAYMENT],
            "awaiting_payment",
        ),
        ([TransactionStatus.RESOLVED_REFUND, TransactionStatus.LOCKED], "sold"),
    ],
)
def test_status_follows_the_transactions(monkeypatch, db, people, statuses, expected) -> None:
    listing = _create_listing(_client(monkeypatch, db, people["seller"]))
    for status in statuses:
        _add_transaction(db, listing["id"], people, status)

    assert listing_status(db, uuid.UUID(listing["id"])) == expected
    assert _client(monkeypatch, db).get(f"/listings/{listing['id']}").json()["status"] == expected


def test_completed_sales_count_released_transactions(monkeypatch, db, people) -> None:
    seller = _client(monkeypatch, db, people["seller"])
    for status in (
        TransactionStatus.RELEASED,
        TransactionStatus.RESOLVED_RELEASE,
        TransactionStatus.REFUNDED_BUYER,
        TransactionStatus.LOCKED,
    ):
        _add_transaction(db, _create_listing(seller)["id"], people, status)

    listing = _create_listing(seller)
    body = _client(monkeypatch, db).get(f"/listings/{listing['id']}").json()
    assert body["seller"]["completed_sales"] == 2


def test_a_sold_item_refuses_new_payments(monkeypatch, db, people) -> None:
    listing = _create_listing(_client(monkeypatch, db, people["seller"]))
    _add_transaction(db, listing["id"], people, TransactionStatus.LOCKED)

    buyer = _client(monkeypatch, db, people["buyer"])
    response = buyer.post("/transactions", json={"listing_id": listing["id"]})

    assert response.status_code == 422
    assert response.json()["detail"] == "This item has already been paid for."


def test_an_unpaid_item_still_takes_payments(monkeypatch, db, people) -> None:
    listing = _create_listing(_client(monkeypatch, db, people["seller"]))
    _add_transaction(db, listing["id"], people, TransactionStatus.AWAITING_PAYMENT)

    buyer = _client(monkeypatch, db, people["buyer"])

    assert buyer.post("/transactions", json={"listing_id": listing["id"]}).status_code == 201


def test_contract_has_the_photo_operations() -> None:
    paths = app.openapi()["paths"]

    assert "post" in paths["/listings/{listing_id}/photos"]
    assert "get" in paths["/listings/{listing_id}/photos/{photo_id}"]
    listing = app.openapi()["components"]["schemas"]["ListingResponse"]["properties"]
    assert {"description", "category", "buyer_name", "status", "photos", "seller"} <= set(listing)
