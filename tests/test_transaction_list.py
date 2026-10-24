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
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.transaction_list import SEGMENT_STATUSES
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


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
                cast(Table, ListingPhoto.__table__),
                cast(Table, Transaction.__table__),
            ],
        )
    finally:
        column.server_default = original
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    try:
        yield session
    finally:
        session.close()


def _listing(seller: User, title: str, price: str, *, minutes: int = 0) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title=title,
        price=Decimal(price),
        is_serialized=False,
        dispute_policy={},
        created_at=NOW + timedelta(minutes=minutes),
        updated_at=NOW + timedelta(minutes=minutes),
    )


def _order(
    listing: Listing,
    buyer: User,
    status: TransactionStatus,
    reference: str,
    *,
    minutes: int,
) -> Transaction:
    at = NOW + timedelta(minutes=minutes)
    return Transaction(
        id=uuid.uuid4(),
        reference=reference,
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        amount=listing.price,
        status=status,
        locked_at=at if status == TransactionStatus.LOCKED else None,
        created_at=at,
        updated_at=at,
    )


@pytest.fixture
def shop(db: Session) -> dict[str, Any]:
    seller = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    other_seller = _user(UserRole.SELLER, "Other Shop", "+254722000000")
    buyer = _user(UserRole.BUYER, "Brian Otieno", "+254712345678")
    other_buyer = _user(UserRole.BUYER, "Wanjiku", "+254733000111")
    admin = _user(UserRole.ADMIN, "Vekro Support", "+254700000001")
    phone = _listing(seller, "iPhone 13, 128GB, Midnight", "52000.00", minutes=1)
    shoes = _listing(seller, "Nike Air Force 1 size 42", "6500.00", minutes=2)
    dress = _listing(seller, "Kitenge wrap dress", "3200.00", minutes=3)
    sofa = _listing(seller, "3-seater sofa", "38000.00", minutes=4)
    unsold = _listing(seller, "HP Pavilion 15 laptop", "68500.00", minutes=5)
    elsewhere = _listing(other_seller, "Someone else's item", "1000.00")
    db.add_all([seller, other_seller, buyer, other_buyer, admin])
    db.flush()
    db.add_all([phone, shoes, dress, sofa, unsold, elsewhere])
    db.flush()
    db.add_all(
        [
            ListingPhoto(
                id=uuid.uuid4(),
                listing_id=phone.id,
                position=1,
                content_type="image/jpeg",
                size_bytes=3,
                data=b"two",
                created_at=NOW,
            ),
            cover := ListingPhoto(
                id=uuid.uuid4(),
                listing_id=phone.id,
                position=0,
                content_type="image/jpeg",
                size_bytes=3,
                data=b"one",
                created_at=NOW,
            ),
        ]
    )
    orders = [
        _order(phone, buyer, TransactionStatus.LOCKED, "VK-7K3P9Q", minutes=10),
        _order(shoes, buyer, TransactionStatus.OUT_FOR_DELIVERY, "VK-3M8D2L", minutes=20),
        _order(dress, other_buyer, TransactionStatus.RELEASED, "VK-8C1Q7T", minutes=30),
        _order(sofa, other_buyer, TransactionStatus.AWAITING_PAYMENT, "VK-5R2W9X", minutes=40),
        _order(elsewhere, buyer, TransactionStatus.LOCKED, "VK-0THER0", minutes=50),
    ]
    db.add_all(orders)
    db.commit()
    return {
        "seller": seller,
        "other_seller": other_seller,
        "buyer": buyer,
        "admin": admin,
        "cover": cover,
        "phone": phone,
        "orders": orders,
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


def _refs(body: dict[str, Any]) -> list[str]:
    return [item["reference"] for item in body["items"]]


def test_seller_sees_only_their_orders_newest_first(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/transactions").json()

    assert _refs(body) == ["VK-5R2W9X", "VK-8C1Q7T", "VK-3M8D2L", "VK-7K3P9Q"]
    assert body["total"] == 4
    first = body["items"][-1]
    assert first["viewer_role"] == "seller"
    assert first["counterpart"] == {"name": "Brian Otieno", "phone_masked": "+254 712 *** 678"}
    assert first["listing"]["title"] == "iPhone 13, 128GB, Midnight"
    assert first["listing"]["photo_url"] == (
        f"/listings/{shop['phone'].id}/photos/{shop['cover'].id}"
    )
    assert first["deadlines"]["dispatch_by"].startswith("2026-10-26T09:10:00")
    assert body["items"][0]["listing"]["photo_url"] is None
    assert "server_time" in body


def test_buyer_sees_orders_they_bought_with_the_seller_as_counterpart(
    monkeypatch, db, shop
) -> None:
    client = _client_as(monkeypatch, db, shop["buyer"])

    body = client.get("/transactions").json()

    assert _refs(body) == ["VK-0THER0", "VK-3M8D2L", "VK-7K3P9Q"]
    assert body["items"][-1]["counterpart"]["name"] == "Amina Gadgets"
    assert body["items"][-1]["viewer_role"] == "buyer"


def test_counts_cover_all_orders_before_filters(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/transactions", params={"segment": "completed", "q": "dress"}).json()

    assert body["counts"] == {
        "needs_action": 2,
        "in_transit": 1,
        "held": 0,
        "completed": 1,
        "issues": 0,
    }
    assert body["status_counts"] == {
        "locked": 1,
        "out_for_delivery": 1,
        "released": 1,
        "awaiting_payment": 1,
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"segment": "needs_action"}, ["VK-3M8D2L", "VK-7K3P9Q"]),
        ({"segment": "in_transit"}, ["VK-5R2W9X"]),
        ({"segment": "held"}, []),
        ({"status": ["locked", "released"]}, ["VK-8C1Q7T", "VK-7K3P9Q"]),
        ({"segment": "needs_action", "status": "locked"}, ["VK-7K3P9Q"]),
        ({"q": "vk-3m8"}, ["VK-3M8D2L"]),
        ({"q": "IPHONE"}, ["VK-7K3P9Q"]),
        ({"q": "100%_"}, []),
        ({"sort": "amount_desc"}, ["VK-7K3P9Q", "VK-5R2W9X", "VK-3M8D2L", "VK-8C1Q7T"]),
        ({"sort": "oldest"}, ["VK-7K3P9Q", "VK-3M8D2L", "VK-8C1Q7T", "VK-5R2W9X"]),
        ({"page": 2, "page_size": 3}, ["VK-7K3P9Q"]),
    ],
)
def test_filters_sort_and_pages(monkeypatch, db, shop, params, expected) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/transactions", params=params).json()

    assert _refs(body) == expected


def test_page_reports_total_matching_filters(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/transactions", params={"page": 2, "page_size": 3}).json()

    assert (body["total"], body["page"], body["page_size"]) == (4, 2, 3)


def test_every_status_belongs_to_exactly_one_segment() -> None:
    seen = [status for members in SEGMENT_STATUSES.values() for status in members]

    assert sorted(seen) == sorted(TransactionStatus)


def test_admins_are_refused(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["admin"])

    assert client.get("/transactions").status_code == 403


def test_requires_a_session(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, None)

    assert client.get("/transactions").status_code == 401


def test_rejects_bad_query_values(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    assert client.get("/transactions", params={"segment": "nope"}).status_code == 422
    assert client.get("/transactions", params={"page_size": 101}).status_code == 422
    assert client.get("/transactions", params={"page": 0}).status_code == 422


def test_seller_lists_their_listings_with_status_counts(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/listings").json()

    assert [item["title"] for item in body["items"]] == [
        "HP Pavilion 15 laptop",
        "3-seater sofa",
        "Kitenge wrap dress",
        "Nike Air Force 1 size 42",
        "iPhone 13, 128GB, Midnight",
    ]
    assert body["counts"] == {"active": 1, "awaiting_payment": 1, "sold": 3, "archived": 0}
    assert body["total"] == 5
    assert body["items"][0]["status"] == "active"
    assert body["items"][-1]["photos"][0]["id"] == str(shop["cover"].id)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "active"}, ["HP Pavilion 15 laptop"]),
        ({"status": "awaiting_payment"}, ["3-seater sofa"]),
        ({"q": "nike"}, ["Nike Air Force 1 size 42"]),
        ({"status": "sold", "page": 2, "page_size": 2}, ["iPhone 13, 128GB, Midnight"]),
    ],
)
def test_listing_filters_and_pages(monkeypatch, db, shop, params, expected) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/listings", params=params).json()

    assert [item["title"] for item in body["items"]] == expected


def test_other_sellers_listings_never_appear(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["other_seller"])

    body = client.get("/listings").json()

    assert [item["title"] for item in body["items"]] == ["Someone else's item"]


def test_buyers_cannot_list_listings(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["buyer"])

    assert client.get("/listings").status_code == 403


def test_lists_are_in_the_openapi_contract() -> None:
    paths = app.openapi()["paths"]
    orders = paths["/transactions"]["get"]["responses"]["200"]["content"]["application/json"]
    listings = paths["/listings"]["get"]["responses"]["200"]["content"]["application/json"]

    assert orders["schema"]["$ref"].endswith("/TransactionListResponse")
    assert listings["schema"]["$ref"].endswith("/ListingListResponse")
