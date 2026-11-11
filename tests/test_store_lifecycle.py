"""M28: open, close and reopen a store (docs/accounts-and-modes.md, section 6).

Closing waits for every order that still needs the seller; it archives listings, stops new
orders and keeps all history. Each change is anchored (store_opened, store_closed) and shows on
the seller's public track record; the full audit checks every store event against its row.
"""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.core.settings import get_settings
from app.models.anchor_event import AnchorEvent
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.escrow import Escrow
from app.models.listing import Listing
from app.models.store_event import StoreEvent
from app.models.transaction import TransactionStatus
from app.models.user import SELLER_TERMS_VERSION, StoreStatus, User, UserRole
from app.schemas.transaction import CreateTransactionRequest
from app.services.anchoring import payloads
from app.services.anchoring.full_audit import run_full_audit
from app.services.anchoring.track_record import seller_track_record
from app.services.payment_prompts import PaymentResendInvalidStateError, resend_payment_prompt
from app.services.store import (
    StoreCloseBlockedError,
    StoreNotAllowedError,
    backfill_store_events,
    close_blockers,
    close_store,
    reopen_store,
    start_selling,
)
from app.services.transaction import (
    TransactionValidationError,
    confirm_payment_callback,
    create_transaction,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tests.accounts import account_fields
from tests.test_anchoring_batcher import _db
from tests.test_anchoring_track_record import _released
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _seed_transaction_context,
)


def _store_events(db: Session, user: User) -> list[AnchorEvent]:
    ids = [
        str(i)
        for i in db.execute(select(StoreEvent.id).where(StoreEvent.user_id == user.id)).scalars()
    ]
    return list(
        db.execute(
            select(AnchorEvent).where(AnchorEvent.source_id.in_(ids)).order_by(AnchorEvent.id)
        ).scalars()
    )


def _settle_payout(db: Session, order) -> None:
    """What the payout executor does once the seller's money has gone out."""
    order.payout_status = "succeeded"
    for escrow in db.execute(select(Escrow).where(Escrow.transaction_id == order.id)).scalars():
        escrow.released_amount = escrow.funded_amount - escrow.refunded_amount
    db.commit()


def _member(db: Session, phone: str) -> User:
    user = User(name="Amina", phone=phone, password_hash="x", **account_fields("buyer"))
    db.add(user)
    db.commit()
    return user


def test_a_buyer_starts_selling_and_it_is_anchored_without_personal_data() -> None:
    db = _db()
    amina = _member(db, "+254711200001")

    start_selling(
        db, amina, shop_name="Amina Baskets", shop_area="Kisumu", terms_version=SELLER_TERMS_VERSION
    )

    assert amina.store_status == StoreStatus.ACTIVE and amina.shop_name == "Amina Baskets"
    assert amina.seller_terms_version == SELLER_TERMS_VERSION
    # Open, but takes no orders until its payout number is verified by SMS.
    assert not amina.store_payout_ready
    [event] = _store_events(db, amina)
    assert event.event_type == payloads.STORE_OPENED
    assert "Amina" not in event.payload and amina.phone not in event.payload
    assert '"reopened":false' in event.payload.replace(" ", "")


def test_start_selling_refusals() -> None:
    db = _db()
    amina = _member(db, "+254711200002")
    with pytest.raises(StoreNotAllowedError):
        start_selling(db, amina, shop_name="A", shop_area=None, terms_version="old")
    start_selling(db, amina, shop_name="Amina", shop_area=None, terms_version=SELLER_TERMS_VERSION)
    with pytest.raises(StoreNotAllowedError, match="already open"):
        start_selling(
            db, amina, shop_name="Amina", shop_area=None, terms_version=SELLER_TERMS_VERSION
        )
    admin = User(name="Ops", phone="+254711200003", password_hash="x", role=UserRole.ADMIN)
    db.add(admin)
    db.commit()
    with pytest.raises(StoreNotAllowedError, match="Admin"):
        start_selling(
            db, admin, shop_name="Ops", shop_area=None, terms_version=SELLER_TERMS_VERSION
        )


def test_closing_waits_for_an_open_order_then_succeeds(monkeypatch) -> None:
    db = _db()
    buyer, seller, listing, order = _seed_transaction_context(
        db, serialized_listing=False, suffix="61"
    )
    confirm_payment_callback(db, payload=_build_callback_payload(order.id))
    assert order.status == TransactionStatus.LOCKED

    blockers = close_blockers(db, seller)
    assert blockers.any and blockers.open_orders == [order.reference]
    with pytest.raises(StoreCloseBlockedError) as refused:
        close_store(db, seller)
    assert refused.value.blockers.open_orders == [order.reference]
    assert seller.store_status == StoreStatus.ACTIVE

    # The order finishes through the real services: nothing blocks any more.
    from app.services import transaction as transaction_service
    from app.services.transaction import (
        confirm_buyer_delivery_otp,
        dispatch_transaction,
        mark_transaction_arrived,
    )

    dispatch_transaction(db, transaction_id=order.id, seller=seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "246810")
    mark_transaction_arrived(db, transaction_id=order.id, seller=seller)
    confirm_buyer_delivery_otp(db, transaction_id=order.id, buyer=buyer, otp_code="246810")
    assert close_blockers(db, seller).held_amount > 0  # released, but not yet paid out
    _settle_payout(db, order)

    assert not close_blockers(db, seller).any
    close_store(db, seller)

    assert seller.store_status == StoreStatus.CLOSED and seller.store_closed_at is not None
    assert db.get(Listing, listing.id).archived_at is not None
    # History stays: the order and its anchored events are untouched.
    assert order.status == TransactionStatus.RELEASED
    kinds = [event.event_type for event in _store_events(db, seller)]
    assert kinds[-1] == payloads.STORE_CLOSED


def test_held_money_unsettled_payouts_and_open_disputes_block_closing(monkeypatch) -> None:
    db = _db()
    order = _released(db, monkeypatch, "62")
    seller = db.get(User, order.seller_id)
    _settle_payout(db, order)
    assert not close_blockers(db, seller).any

    # Money still held for one of the store's orders.
    if db.execute(select(Escrow).where(Escrow.transaction_id == order.id)).first() is None:
        db.add(
            Escrow(
                id=uuid.uuid4(),
                transaction_id=order.id,
                buyer_id=order.buyer_id,
                seller_id=seller.id,
                amount_total=Decimal("100.00"),
                custody_provider="simulated",
            )
        )
        db.commit()
    escrow = db.execute(select(Escrow).where(Escrow.transaction_id == order.id)).scalar_one()
    escrow.funded_amount, escrow.released_amount, escrow.refunded_amount = (
        Decimal("100.00"),
        Decimal("40.00"),
        Decimal("0.00"),
    )
    db.commit()
    assert close_blockers(db, seller).held_amount == Decimal("60.00")
    escrow.released_amount = Decimal("100.00")
    db.commit()

    order.payout_status = "unknown"
    db.commit()
    assert close_blockers(db, seller).unsettled_payouts == [order.reference]
    order.payout_status = "succeeded"
    db.commit()

    db.add(
        Dispute(
            id=uuid.uuid4(),
            transaction_id=order.id,
            opened_by_user_id=order.buyer_id,
            dispute_type=DisputeType.FUNCTIONAL,
            status=DisputeStatus.OPEN,
            reason="other",
            description="Not as described",
        )
    )
    db.commit()
    assert close_blockers(db, seller).open_disputes == [order.reference]


def test_a_closed_store_takes_no_orders_and_its_unpaid_orders_cannot_be_paid() -> None:
    db = _db()
    buyer, seller, listing, unpaid = _seed_transaction_context(
        db, serialized_listing=False, suffix="63"
    )
    assert unpaid.status == TransactionStatus.AWAITING_PAYMENT
    # An unpaid order holds no money: it doesn't block closing.
    close_store(db, seller)

    with pytest.raises(PaymentResendInvalidStateError, match="This store is closed."):
        resend_payment_prompt(db, transaction_id=unpaid.id, buyer=buyer)

    other_buyer = _member(db, "+254711200004")
    fresh = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Another basket",
        price=Decimal("900.00"),
        is_serialized=False,
        dispute_policy={},
    )
    db.add(fresh)
    db.commit()
    with pytest.raises(TransactionValidationError, match="This store is closed."):
        create_transaction(db, other_buyer, CreateTransactionRequest(listing_id=fresh.id))


def test_reopen_keeps_listings_archived_and_anchors_the_reopening() -> None:
    db = _db()
    _buyer, seller, listing, _unpaid = _seed_transaction_context(
        db, serialized_listing=False, suffix="64"
    )
    close_store(db, seller)
    with pytest.raises(StoreNotAllowedError):
        close_store(db, seller)

    reopen_store(db, seller)

    assert seller.store_status == StoreStatus.ACTIVE and seller.store_closed_at is None
    assert db.get(Listing, listing.id).archived_at is not None
    last = _store_events(db, seller)[-1]
    assert last.event_type == payloads.STORE_OPENED and '"reopened":true' in last.payload
    with pytest.raises(StoreNotAllowedError):
        reopen_store(db, seller)


def test_backfill_records_stores_from_before_m28_once_and_the_audit_checks_them() -> None:
    db = _db()
    old = User(
        name="Wanjiru",
        phone="+254711200005",
        password_hash="x",
        **account_fields("seller"),
    )
    closed = User(
        name="Juma",
        phone="+254711200006",
        password_hash="x",
        **{**account_fields("seller"), "store_status": StoreStatus.CLOSED},
        store_closed_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db.add_all([old, closed])
    db.commit()
    assert db.execute(select(StoreEvent)).first() is None

    assert backfill_store_events(db) == 2
    assert backfill_store_events(db) == 0
    assert [e.event_type for e in _store_events(db, closed)] == [
        payloads.STORE_OPENED,
        payloads.STORE_CLOSED,
    ]
    assert all('"backfilled":true' in e.payload for e in _store_events(db, old))
    assert run_full_audit(db).verdict == "CLEAN"

    # Editing a store event after it was anchored is caught.
    db.execute(
        update(StoreEvent)
        .where(StoreEvent.user_id == old.id)
        .values(at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    db.commit()
    codes = {finding.code for finding in run_full_audit(db).findings}
    assert "source_row_changed" in codes


def test_the_track_record_lists_the_store_history() -> None:
    db = _db()
    _buyer, seller, _listing, _unpaid = _seed_transaction_context(
        db, serialized_listing=False, suffix="65"
    )
    backfill_store_events(db)
    close_store(db, seller)
    reopen_store(db, seller)

    record = seller_track_record(db, seller.id, get_settings())

    types = [json.loads(item["payload"])["type"] for item in record["store_history"]]
    assert types == [payloads.STORE_OPENED, payloads.STORE_CLOSED, payloads.STORE_OPENED]


# --- HTTP -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    from app.main import app

    app.dependency_overrides.clear()


@pytest.fixture
def http_db():
    """One shared connection (StaticPool), so the test client's threads see the same database.
    No anchor_events table: anchoring is covered by the tests above."""
    from typing import Any, cast

    from app.db.base import Base
    from app.models.listing_photo import ListingPhoto
    from app.models.notification import Notification
    from app.models.payment_prompt import PaymentPrompt
    from app.models.transaction import Transaction
    from sqlalchemy import Table, create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from tests.sqlite_tables import create_disputes_table

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
                    Listing,
                    ListingPhoto,
                    Transaction,
                    Notification,
                    PaymentPrompt,
                    Escrow,
                    StoreEvent,
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


def test_the_store_endpoints(monkeypatch, http_db) -> None:
    from app.models.transaction import Transaction

    from tests.test_accounts_and_modes import _client_as, _listing, _user

    db = http_db
    buyer = _user("buyer", "+254711200010")
    seller = _user("seller", "+254711200011")
    listing = _listing(seller)
    db.add_all([buyer, seller, listing])
    db.commit()
    order = Transaction(
        id=uuid.uuid4(),
        reference="VK-STORE1",
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("1500.00"),
        status=TransactionStatus.LOCKED,
    )
    db.add(order)
    db.commit()

    # A buyer opens a store; the terms must be the current ones; only once.
    amina = _member(db, "+254711200007")
    client = _client_as(monkeypatch, db, amina)
    body = {"shop_name": "Amina Baskets", "terms_version": SELLER_TERMS_VERSION}
    assert client.post("/account/store", json={**body, "terms_version": "x"}).status_code == 422
    opened = client.post("/account/store", json=body)
    assert opened.status_code == 201
    assert opened.json()["status"] == "active" and opened.json()["payout_ready"] is False
    assert client.post("/account/store", json=body).status_code == 409
    assert client.get("/auth/me").json()["role"] == "seller"

    admin = User(name="Ops", phone="+254711200008", password_hash="x", role=UserRole.ADMIN)
    db.add(admin)
    db.commit()
    assert _client_as(monkeypatch, db, admin).post("/account/store", json=body).status_code == 403

    # Closing is refused while the order is open, and says what must finish first.
    client = _client_as(monkeypatch, db, seller)
    check = client.get("/account/store/close-check").json()
    assert check["can_close"] is False and check["open_orders"] == [order.reference]
    refused = client.post("/account/store/close")
    assert refused.status_code == 409
    assert refused.json()["open_orders"] == [order.reference]
    assert refused.json()["detail"] == "Finish these first."

    # The order finishes; the store closes; the Trust Link says so; history stays readable.
    order.status = TransactionStatus.RELEASED  # no anchoring in this database
    _settle_payout(db, order)
    closed = client.post("/account/store/close")
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    link = client.get(f"/listings/{listing.id}").json()
    assert link["store_status"] == "closed" and link["store_ready"] is False
    assert client.get("/transactions?as=selling").status_code == 200
    assert (
        client.post(
            "/listings", json={"title": "New", "price": "10", "is_serialized": False}
        ).status_code
        == 403
    )

    reopened = client.post("/account/store/reopen")
    assert reopened.status_code == 200 and reopened.json()["status"] == "active"
    assert client.post("/account/store/reopen").status_code == 409
