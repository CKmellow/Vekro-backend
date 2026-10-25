import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app.db.base import Base
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction
from app.models.transaction import TransactionStatus as S
from app.models.user import User, UserRole
from app.services.seller_analytics import seller_analytics
from sqlalchemy import Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from tests.sqlite_tables import create_disputes_table


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    policy = cast(Any, Listing.__table__.c.dispute_policy)
    original = policy.server_default
    policy.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                cast(Table, t)
                for t in (
                    User.__table__,
                    Listing.__table__,
                    ListingPhoto.__table__,
                    Transaction.__table__,
                )
            ],
        )
    finally:
        policy.server_default = original
    create_disputes_table(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _setup(db: Session):
    seller = User(name="Amina", phone="+254700000001", role=UserRole.SELLER, password_hash="x")
    other = User(name="Other", phone="+254700000002", role=UserRole.SELLER, password_hash="x")
    buyer = User(name="Brian", phone="+254700000003", role=UserRole.BUYER, password_hash="x")
    db.add_all([seller, other, buyer])
    db.flush()
    phone = Listing(
        seller_id=seller.id, title="iPhone 13", price=Decimal("52000"), dispute_policy={}
    )
    shoes = Listing(
        seller_id=seller.id, title="Air Force 1", price=Decimal("6500"), dispute_policy={}
    )
    theirs = Listing(seller_id=other.id, title="Theirs", price=Decimal("1000"), dispute_policy={})
    db.add_all([phone, shoes, theirs])
    db.flush()
    db.add(
        ListingPhoto(
            listing_id=phone.id, position=0, content_type="image/jpeg", size_bytes=1, data=b"x"
        )
    )
    return seller, other, buyer, phone, shoes, theirs


def _order(db, listing, buyer, status, *, days_ago, amount, dispatched_hours=None, released=True):
    placed = NOW - timedelta(days=days_ago)
    t = Transaction(
        id=uuid.uuid4(),
        reference=f"VK-{uuid.uuid4().hex[:6].upper()}",
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        amount=Decimal(amount),
        status=status,
        created_at=placed,
        updated_at=placed,
        locked_at=placed,
        dispatched_at=placed + timedelta(hours=dispatched_hours) if dispatched_hours else None,
        released_at=placed + timedelta(days=1) if status in {S.RELEASED} and released else None,
    )
    db.add(t)
    return t


def test_seller_analytics(db: Session) -> None:
    seller, _other, buyer, phone, shoes, theirs = _setup(db)
    _order(db, phone, buyer, S.RELEASED, days_ago=3, amount="52000", dispatched_hours=6)
    _order(db, shoes, buyer, S.RELEASED, days_ago=5, amount="6500", dispatched_hours=2)
    refunded = _order(db, shoes, buyer, S.REFUNDED_BUYER, days_ago=6, amount="6500")
    _order(db, shoes, buyer, S.AWAITING_PAYMENT, days_ago=1, amount="6500")  # unpaid: ignored
    _order(db, phone, buyer, S.RELEASED, days_ago=40, amount="50000")  # previous period
    _order(db, theirs, buyer, S.RELEASED, days_ago=2, amount="1000")  # another seller
    db.add(
        Dispute(
            transaction_id=refunded.id,
            opened_by_user_id=buyer.id,
            dispute_type=DisputeType.FUNCTIONAL,
            status=DisputeStatus.OPEN,
            reason="not_working",
            description="Dead on arrival",
            evidence={},
        )
    )
    db.commit()

    a = seller_analytics(db, seller.id, "30d", now=NOW)
    assert a.range_days == 30 and a.bucket == "day" and len(a.sales_series) == 30
    assert a.completed_sales == 3
    assert a.current.orders == 3
    assert a.current.sales_volume == Decimal("58500")
    assert a.current.average_order_value == Decimal("21667")
    assert a.current.completion_rate == round(2 / 3, 4)
    assert a.current.dispute_rate == round(1 / 3, 4)
    assert a.current.average_dispatch_seconds == 4 * 3600
    assert a.previous.orders == 1 and a.previous.sales_volume == Decimal("50000")
    assert a.dispute_categories == [("not_working", 1)]
    assert sum(w[3] for w in a.outcomes) == 1 and sum(w[1] for w in a.outcomes) == 2
    top = a.top_listings
    assert [t.title for t in top] == ["Air Force 1", "iPhone 13"]
    assert top[1].photo_url and top[1].photo_url.startswith(f"/listings/{phone.id}/photos/")
    assert top[0].sales == Decimal("6500") and top[0].completion_rate == 0.5

    weekly = seller_analytics(db, seller.id, "90d", now=NOW)
    assert weekly.bucket == "week" and len(weekly.sales_series) in (13, 14)


def test_empty_seller(db: Session) -> None:
    seller, *_ = _setup(db)
    db.commit()
    a = seller_analytics(db, seller.id, "7d", now=NOW)
    assert a.completed_sales == 0 and a.current.orders == 0
    assert a.current.completion_rate is None and a.current.average_order_value is None
    assert a.top_listings == [] and len(a.sales_series) == 7
