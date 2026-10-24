from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.main import app
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from app.services.transaction_summary import seller_summary
from sqlalchemy.orm import Session

from tests.test_transaction_list import _client_as, _listing, _order, _user
from tests.test_transaction_list import db as _db_fixture  # noqa: F401  (shared SQLite fixture)

db = _db_fixture

# 1 Nov 2026 00:30 EAT is still 31 Oct in UTC: month boundaries follow Nairobi time.
NOW = datetime(2026, 10, 31, 21, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def shop(db: Session) -> dict[str, Any]:
    seller = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    other = _user(UserRole.SELLER, "Other Shop", "+254722000000")
    buyer = _user(UserRole.BUYER, "Brian Otieno", "+254712345678")
    db.add_all([seller, other, buyer])
    db.flush()

    def sold(price: str, status: TransactionStatus, ref: str, at: datetime, owner=seller):
        listing = _listing(owner, f"Item {ref}", price)
        db.add(listing)
        db.flush()
        order = _order(listing, buyer, status, ref, minutes=0)
        order.updated_at = at
        if status == TransactionStatus.HOLD_24H:
            order.hold_started_at = at
        db.add(order)
        return order

    sold("52000.00", TransactionStatus.LOCKED, "VK-LOCK01", NOW)
    sold("6500.00", TransactionStatus.HOLD_24H, "VK-HOLD02", NOW - timedelta(hours=3))
    early = sold("3000.00", TransactionStatus.HOLD_24H, "VK-HOLD01", NOW - timedelta(hours=20))
    sold("68500.00", TransactionStatus.RETURN_RECEIVED, "VK-RETN01", NOW)
    # Released 1 Nov 00:10 EAT (this month) and 31 Oct 23:50 EAT (last month).
    sold(
        "3200.00",
        TransactionStatus.RELEASED,
        "VK-REL001",
        datetime(2026, 10, 31, 21, 10, tzinfo=UTC),
    )
    sold(
        "38000.00",
        TransactionStatus.RESOLVED_RELEASE,
        "VK-REL002",
        datetime(2026, 10, 31, 20, 50, tzinfo=UTC),
    )
    sold("9000.00", TransactionStatus.RELEASED, "VK-REL003", datetime(2026, 9, 15, tzinfo=UTC))
    sold("1000.00", TransactionStatus.AWAITING_PAYMENT, "VK-WAIT01", NOW)
    sold("500.00", TransactionStatus.REFUNDED_BUYER, "VK-REFD01", NOW)
    sold("99999.00", TransactionStatus.LOCKED, "VK-OTHER1", NOW, owner=other)
    db.commit()
    return {"seller": seller, "buyer": buyer, "early": early}


def test_totals_follow_eat_months_and_only_this_seller(db, shop) -> None:
    summary = seller_summary(db, seller=shop["seller"], now=NOW)

    assert (summary.in_escrow.amount, summary.in_escrow.orders) == (Decimal("130000.00"), 4)
    assert (summary.released_this_month.amount, summary.released_this_month.orders) == (
        Decimal("3200.00"),
        1,
    )
    assert (summary.released_last_month.amount, summary.released_last_month.orders) == (
        Decimal("38000.00"),
        1,
    )
    assert summary.awaiting_dispatch == 1
    assert summary.open_issues == 1
    assert summary.next_release == (shop["early"].id, "VK-HOLD01")


def test_endpoint_returns_the_summary(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    body = client.get("/transactions/summary").json()

    assert body["in_escrow"] == {"amount": "130000.00", "orders": 4}
    assert body["awaiting_dispatch"] == 1
    assert body["next_release"]["reference"] == "VK-HOLD01"
    assert "server_time" in body


def test_buyers_are_refused(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["buyer"])

    assert client.get("/transactions/summary").status_code == 403


def test_empty_shop_has_zero_totals(db) -> None:
    seller = _user(UserRole.SELLER, "New Shop", "+254722555555")
    db.add(seller)
    db.commit()

    summary = seller_summary(db, seller=seller, now=NOW)

    assert summary.in_escrow.orders == 0
    assert summary.in_escrow.amount == Decimal("0.00")
    assert summary.next_release is None
