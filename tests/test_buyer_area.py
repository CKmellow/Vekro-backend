"""M28 buyer area endpoints: Orders filters, Home ("what needs you now"), Profile & security."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app.core.settings import get_settings
from app.db.base import Base
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.notification import Notification
from app.models.payment_prompt import PaymentPrompt
from app.models.phone_code import PhoneCode
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import PartyRole, User, UserRole
from app.models.user_session import UserSession
from app.services import phone_auth
from sqlalchemy import Table, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.sqlite_tables import create_disputes_table
from tests.test_accounts_and_modes import _client_as, _listing, _user

NOW = datetime.now(UTC)


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
                cast(Table, model.__table__)
                for model in (
                    User,
                    UserSession,
                    PhoneCode,
                    Listing,
                    ListingPhoto,
                    Transaction,
                    Notification,
                    PaymentPrompt,
                )
            ],
        )
    finally:
        for column, default in originals.items():
            column.server_default = default
    create_disputes_table(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch) -> Iterator[None]:
    phone_auth._CLIENT_BUCKETS.clear()
    phone_auth._DECOY_BUCKETS.clear()
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)
    yield
    app.dependency_overrides.clear()


_refs = iter(range(100, 999))


def _order(
    db: Session, buyer: User, seller: User, status: TransactionStatus, **times
) -> Transaction:
    listing = _listing(seller, f"Item {status.value}")
    db.add(listing)
    order = Transaction(
        id=uuid.uuid4(),
        reference=f"VK-T{next(_refs)}",
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("1000.00"),
        status=status,
        created_at=NOW - timedelta(minutes=next(_refs)),
        updated_at=NOW,
        **times,
    )
    db.add(order)
    db.commit()
    return order


@pytest.fixture
def shop(db: Session) -> dict[str, Any]:
    # Wanjiru has a store and also buys from Juma; Amina buys from Wanjiru.
    wanjiru = _user(PartyRole.SELLER, "+254722300001")
    juma = _user(PartyRole.SELLER, "+254722300002")
    amina = _user(PartyRole.BUYER, "+254712300003")
    db.add_all([wanjiru, juma, amina])
    db.commit()
    orders = {
        "pay": _order(db, wanjiru, juma, TransactionStatus.AWAITING_PAYMENT),
        "door": _order(
            db, wanjiru, juma, TransactionStatus.AT_DOOR_PENDING_INSPECTION, at_door_at=NOW
        ),
        "hold": _order(
            db,
            wanjiru,
            juma,
            TransactionStatus.HOLD_24H,
            hold_started_at=NOW - timedelta(hours=23, minutes=30),
        ),
        "return": _order(db, wanjiru, juma, TransactionStatus.AWAITING_BUYER_RECONFIRMATION),
        "done": _order(db, wanjiru, juma, TransactionStatus.RELEASED),
        "refund": _order(db, wanjiru, juma, TransactionStatus.REFUNDED_BUYER),
        "sale": _order(db, amina, wanjiru, TransactionStatus.LOCKED),
    }
    db.add(
        Dispute(
            id=uuid.uuid4(),
            transaction_id=orders["refund"].id,
            opened_by_user_id=wanjiru.id,
            dispute_type=DisputeType.FUNCTIONAL,
            status=DisputeStatus.RESOLVED_REFUND,
            reason="other",
            description="Stopped working",
        )
    )
    db.commit()
    return {"wanjiru": wanjiru, "juma": juma, "amina": amina, **orders}


def _refs_of(response) -> set[str]:
    return {item["reference"] for item in response.json()["items"]}


def test_buyer_filters_and_counts(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["wanjiru"])
    ref = {key: shop[key].reference for key in ("pay", "door", "hold", "return", "done", "refund")}

    everything = client.get("/transactions?as=buying")
    assert _refs_of(everything) == set(ref.values())  # never the store's own sale
    assert everything.json()["filter_counts"] == {
        "active": 3,
        "completed": 1,
        "refunded": 1,
        "disputed": 1,
        "cases": 1,
    }
    assert _refs_of(client.get("/transactions?as=buying&filter=active")) == {
        ref["pay"],
        ref["door"],
        ref["hold"],
    }
    assert _refs_of(client.get("/transactions?as=buying&filter=completed")) == {ref["done"]}
    assert _refs_of(client.get("/transactions?as=buying&filter=refunded")) == {ref["refund"]}
    assert _refs_of(client.get("/transactions?as=buying&filter=disputed")) == {ref["return"]}
    # Returns & disputes: open and past cases (the refund came from a dispute).
    assert _refs_of(client.get("/transactions?as=buying&filter=cases")) == {ref["refund"]}
    # Search by reference.
    found = client.get(f"/transactions?as=buying&q={ref['hold'].lower()}")
    assert _refs_of(found) == {ref["hold"]}
    # The selling side has no buyer filter counts.
    assert client.get("/transactions?as=selling").json()["filter_counts"] is None


def test_home_shows_what_needs_the_buyer_soonest_first(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["wanjiru"])
    home = client.get("/buyer/home").json()

    kinds = [(item["kind"], item["order"]["reference"]) for item in home["needs_you"]]
    # The 24-hour check ends in 30 minutes, the door in an hour; both before the undated ones.
    assert {kind for kind, _ in kinds} == {"pay", "at_door", "hold_check", "confirm_return"}
    dated = [item for item in home["needs_you"] if item["due_at"]]
    assert [item["kind"] for item in dated] == sorted(
        [item["kind"] for item in dated],
        key=lambda kind: {"hold_check": 0, "at_door": 1}[kind],
    )
    assert home["needs_you"][-1]["due_at"] is None
    assert all(item["order"]["viewer_role"] == "buyer" for item in home["needs_you"])
    assert shop["sale"].reference not in {item["reference"] for item in home["recent"]}
    assert len(home["recent"]) == 5
    assert home["counts"]["active"] == 3


def test_admins_have_no_buyer_area(monkeypatch, db) -> None:
    admin = _user(UserRole.ADMIN, "+254700300009")
    db.add(admin)
    db.commit()
    assert _client_as(monkeypatch, db, admin).get("/buyer/home").status_code == 403


def test_profile_name_and_phone_change(monkeypatch, db, shop) -> None:
    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", True)
    monkeypatch.setattr("app.routers.account.run_in_background", lambda job: job())
    amina = shop["amina"]
    other = UserSession(
        id=uuid.uuid4(),
        user_id=amina.id,
        session_token_hash="other-device",
        csrf_token_hash="other-csrf",
        expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(other)
    db.commit()
    client = _client_as(monkeypatch, db, amina)

    renamed = client.patch("/account/profile", json={"name": "Amina Otieno"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Amina Otieno"

    same = client.post("/account/phone/request-code", json={"phone": amina.phone})
    assert same.status_code == 422
    taken = client.post("/account/phone/request-code", json={"phone": shop["juma"].phone})
    assert taken.status_code == 409

    sent = client.post("/account/phone/request-code", json={"phone": "0712300099"})
    assert sent.status_code == 202
    code = sent.json()["dev_code"]
    wrong = "000000" if code != "000000" else "111111"
    bad = client.put("/account/phone", json={"phone": "0712300099", "code": wrong})
    assert bad.status_code == 422
    changed = client.put("/account/phone", json={"phone": "0712300099", "code": code})
    assert changed.status_code == 200 and changed.json()["phone"] == "+254712300099"
    assert db.get(User, amina.id).phone == "+254712300099"
    # Every other device is signed out.
    assert db.execute(select(UserSession).where(UserSession.id == other.id)).scalar_one().revoked_at
    # Single use.
    again = client.put("/account/phone", json={"phone": "0712300099", "code": code})
    assert again.status_code == 422  # now the same number
