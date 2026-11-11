"""M28: one account for buying and selling (docs/accounts-and-modes.md).

Authorization matrix (member without a store, active store, closed store, admin), the
self-purchase and payout-number bans, store readiness, lists by side and notification audience.
"""

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
from app.models.notification import Notification, NotificationChannel, NotificationEventType
from app.models.payment_prompt import PaymentPrompt
from app.models.phone_code import PhoneCode
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import PartyRole, StoreStatus, User, UserRole
from app.models.user_session import UserSession
from app.services.auth import ActiveSessionContext
from app.services.phone_auth import request_phone_code
from app.services.seller_profile import change_payout
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.accounts import account_fields
from tests.sqlite_tables import create_disputes_table


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def _user(role: Any, phone: str, *, mpesa_phone: str | None = None, **extra) -> User:
    fields = {**account_fields(role), **extra}
    if fields.get("store_status") == StoreStatus.ACTIVE and mpesa_phone is None:
        mpesa_phone = phone
    return User(
        id=uuid.uuid4(),
        name=f"{getattr(role, 'value', role)} user",
        phone=phone,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=mpesa_phone,
        session_version=1,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
        **fields,
    )


def _listing(seller: User, title: str = "Kiondo basket") -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title=title,
        price=Decimal("1500.00"),
        is_serialized=False,
        unique_id=None,
        dispute_policy={},
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
                cast(Table, ListingPhoto.__table__),
                cast(Table, Transaction.__table__),
                cast(Table, Notification.__table__),
                cast(Table, PaymentPrompt.__table__),
                cast(Table, PhoneCode.__table__),
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
    buyer = _user(PartyRole.BUYER, "+254712000001")
    seller = _user(PartyRole.SELLER, "+254722000002")
    other_seller = _user(PartyRole.SELLER, "+254722000003")
    closed = _user(
        PartyRole.SELLER,
        "+254722000004",
        store_status=StoreStatus.CLOSED,
        store_closed_at=NOW,
    )
    admin = _user(UserRole.ADMIN, "+254700000005")
    listing = _listing(seller)
    other_listing = _listing(other_seller, "Leather sandals")
    db.add_all([buyer, seller, other_seller, closed, admin, listing, other_listing])
    db.commit()
    return {
        "buyer": buyer,
        "seller": seller,
        "other_seller": other_seller,
        "closed": closed,
        "admin": admin,
        "listing": listing,
        "other_listing": other_listing,
    }


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


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
    client.cookies.set(get_settings().csrf_cookie_name, "csrf-token")
    client.headers[get_settings().csrf_header_name] = "csrf-token"
    return client


def _buy(client: TestClient, listing: Listing):
    return client.post("/transactions", json={"listing_id": str(listing.id)})


# --- authorization matrix -------------------------------------------------------------------

NEW_LISTING = {"title": "Kitenge dress", "price": "2500.00", "is_serialized": False}

# (route group, method, path, body) -> who passes. Passing means "not refused by the role
# check" (403); the call may still fail later for its own reasons.
MATRIX = [
    ("buying", "get", "/protected/buyer", None, {"buyer", "seller", "closed"}),
    ("buying", "get", "/transactions?as=buying", None, {"buyer", "seller", "closed"}),
    ("store history", "get", "/transactions?as=selling", None, {"seller", "closed"}),
    ("store history", "get", "/transactions/summary", None, {"seller", "closed"}),
    ("store history", "get", "/listings", None, {"seller", "closed"}),
    ("store history", "get", "/protected/seller", None, {"seller", "closed"}),
    ("store changes", "post", "/listings", NEW_LISTING, {"seller"}),
    ("admin", "get", "/protected/admin", None, {"admin"}),
]


@pytest.mark.parametrize(("group", "method", "path", "body", "allowed"), MATRIX)
def test_authorization_matrix(monkeypatch, db, world, group, method, path, body, allowed) -> None:
    for who in ("buyer", "seller", "closed", "admin"):
        client = _client_as(monkeypatch, db, world[who])
        kwargs = {"json": body} if body is not None else {}
        status = getattr(client, method)(path, **kwargs).status_code
        if who in allowed:
            assert status != 403, f"{who} refused on {group}: {method.upper()} {path}"
        else:
            assert status == 403, f"{who} allowed on {group}: {method.upper()} {path} ({status})"


def test_every_member_can_buy_but_an_admin_cannot(monkeypatch, db, world) -> None:
    for who in ("buyer", "closed"):
        assert _buy(_client_as(monkeypatch, db, world[who]), world["listing"]).status_code == 201
        world["listing"] = _listing(world["seller"], f"Basket for {who}")
        db.add(world["listing"])
        db.commit()
    refused = _buy(_client_as(monkeypatch, db, world["admin"]), world["listing"])
    assert refused.status_code == 403


# --- a seller buys from another shop; never from their own ----------------------------------


def test_a_seller_buys_from_another_shop(monkeypatch, db, world) -> None:
    client = _client_as(monkeypatch, db, world["seller"])
    created = _buy(client, world["other_listing"])
    assert created.status_code == 201
    order_id = created.json()["id"]

    bought = client.get("/transactions?as=buying").json()["items"]
    assert [item["id"] for item in bought] == [order_id]
    assert bought[0]["viewer_role"] == "buyer"
    # Without ?as= an account with a store sees its sales, as before M28.
    assert client.get("/transactions").json()["items"] == []
    detail = client.get(f"/transactions/{order_id}").json()
    assert detail["viewer_role"] == "buyer"

    other = _client_as(monkeypatch, db, world["other_seller"])
    sold = other.get("/transactions?as=selling").json()["items"]
    assert [item["id"] for item in sold] == [order_id]
    assert sold[0]["viewer_role"] == "seller"


def test_a_seller_cannot_buy_from_their_own_store(monkeypatch, db, world) -> None:
    client = _client_as(monkeypatch, db, world["seller"])
    response = _buy(client, world["listing"])
    assert response.status_code == 422
    assert response.json()["detail"] == "You can't buy from your own store."
    assert db.query(Transaction).count() == 0

    listing = client.get(f"/listings/{world['listing'].id}").json()
    assert listing["is_own_listing"] is True
    stranger = _client_as(monkeypatch, db, world["buyer"]).get(f"/listings/{world['listing'].id}")
    assert stranger.json()["is_own_listing"] is False


def test_the_database_refuses_an_order_between_one_account_and_itself(db, world) -> None:
    seller = world["seller"]
    db.add(
        Transaction(
            id=uuid.uuid4(),
            reference="VK-SELF01",
            listing_id=world["listing"].id,
            buyer_id=seller.id,
            seller_id=seller.id,
            amount=Decimal("1500.00"),
            status=TransactionStatus.AWAITING_PAYMENT,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(IntegrityError, match="ck_transactions_distinct_parties"):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("field", ["phone", "mpesa_phone"])
def test_no_buying_through_the_stores_payout_number(monkeypatch, db, world, field) -> None:
    payout = "+254733555555"
    world["seller"].mpesa_phone = payout
    buyer = _user(PartyRole.BUYER, "+254712999999")
    setattr(buyer, field, payout)
    db.add(buyer)
    db.commit()

    response = _buy(_client_as(monkeypatch, db, buyer), world["listing"])

    assert response.status_code == 422
    assert response.json()["detail"] == "You can't buy from a store paid out to this number."


# --- store readiness: an SMS-verified payout number -----------------------------------------


@pytest.mark.parametrize("missing", ["number", "verification"])
def test_a_store_without_a_verified_payout_number_takes_no_orders(
    monkeypatch, db, world, missing
) -> None:
    seller = world["seller"]
    if missing == "number":
        seller.mpesa_phone = None
    seller.payout_verified_at = None
    db.commit()

    public = _client_as(monkeypatch, db, None).get(f"/listings/{world['listing'].id}").json()
    assert public["store_ready"] is False and public["status"] == "active"

    response = _buy(_client_as(monkeypatch, db, world["buyer"]), world["listing"])
    assert response.status_code == 422
    assert response.json()["detail"] == "This store isn't ready yet."

    me = _client_as(monkeypatch, db, seller).get("/auth/me").json()
    assert me["store"]["status"] == "active" and me["store"]["payout_ready"] is False
    assert me["role"] == "seller"


def test_a_ready_store_shows_as_ready(monkeypatch, db, world) -> None:
    public = _client_as(monkeypatch, db, None).get(f"/listings/{world['listing'].id}").json()
    assert public["store_ready"] is True and public["is_own_listing"] is False


def _code(db: Session, phone: str) -> str:
    return request_phone_code(db, phone, purpose="payout").code


def test_only_an_sms_code_verifies_the_payout_number(db, world) -> None:
    seller = _user(PartyRole.SELLER, "+254722000010", payout_verified_at=None)
    db.add(seller)
    db.commit()
    assert not seller.store_payout_ready

    # Onboarding (no orders yet) may save a number without a code, but it stays unverified.
    change_payout(db, seller, mpesa_phone="+254722000011", mpesa_account_name="A", code=None)
    assert seller.mpesa_phone == "+254722000011" and not seller.store_payout_ready

    # A code verifies, including for the number already saved.
    code = _code(db, "+254722000011")
    change_payout(db, seller, mpesa_phone="+254722000011", mpesa_account_name="A", code=code)
    assert seller.store_payout_ready

    # Changing the number without a code clears the proof.
    change_payout(db, seller, mpesa_phone="+254722000012", mpesa_account_name="A", code=None)
    assert seller.mpesa_phone == "+254722000012" and not seller.store_payout_ready


# --- auth responses and notifications -------------------------------------------------------


def test_me_describes_the_account_and_its_store(monkeypatch, db, world) -> None:
    buyer = _client_as(monkeypatch, db, world["buyer"]).get("/auth/me").json()
    assert (buyer["role"], buyer["account_type"]) == ("buyer", "member")
    assert buyer["store"]["status"] == "none"

    seller = _client_as(monkeypatch, db, world["seller"]).get("/auth/me").json()
    assert (seller["role"], seller["account_type"]) == ("seller", "member")
    assert seller["store"]["status"] == "active" and seller["store"]["payout_ready"] is True

    closed = _client_as(monkeypatch, db, world["closed"]).get("/auth/me").json()
    assert (closed["role"], closed["store"]["status"]) == ("buyer", "closed")

    admin = _client_as(monkeypatch, db, world["admin"]).get("/auth/me").json()
    assert (admin["role"], admin["account_type"], admin["store"]) == ("admin", "admin", None)


def test_admin_accounts_can_never_hold_a_store(db, world) -> None:
    world["admin"].store_status = StoreStatus.ACTIVE
    with pytest.raises(IntegrityError, match="ck_users_admin_has_no_store"):
        db.commit()
    db.rollback()


def test_notifications_say_which_side_they_are_for(monkeypatch, db, world) -> None:
    seller = world["seller"]
    client = _client_as(monkeypatch, db, seller)
    bought = _buy(client, world["other_listing"]).json()["id"]
    sold = _buy(_client_as(monkeypatch, db, world["buyer"]), world["listing"]).json()["id"]
    for order_id, title in ((bought, "You paid"), (sold, "You sold")):
        db.add(
            Notification(
                id=uuid.uuid4(),
                user_id=seller.id,
                transaction_id=uuid.UUID(order_id),
                event_type=NotificationEventType.TRANSACTION_CREATED,
                channel=NotificationChannel.IN_APP,
                title=title,
                message=title,
                payload={},
                is_read=False,
                created_at=NOW,
            )
        )
    db.commit()
    client = _client_as(monkeypatch, db, seller)  # the buyer's call above switched the session

    def titles(audience: str | None = None) -> dict[str, str | None]:
        query = f"?channel=in_app&audience={audience}" if audience else "?channel=in_app"
        return {
            item["title"]: item["audience"]
            for item in client.get(f"/notifications{query}").json()
            if item["title"] in ("You paid", "You sold")
        }

    assert titles() == {"You paid": "buying", "You sold": "selling"}
    assert titles("buying") == {"You paid": "buying"}
    assert titles("selling") == {"You sold": "selling"}
    counts = {
        side: client.get(f"/notifications/unread-count?audience={side}").json()["unread_count"]
        for side in ("buying", "selling")
    }
    assert counts["buying"] >= 1 and counts["selling"] >= 1
