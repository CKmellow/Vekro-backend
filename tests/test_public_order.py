import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.main import app
from app.models.listing import Listing
from app.models.transaction import Transaction, TransactionStatus
from app.routers import public_orders as router
from app.services import public_order
from app.services.public_order import PublicLookupRateLimitedError, check_lookup_quota, state_group
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset() -> None:
    public_order._BUCKETS.clear()


def _order(status: TransactionStatus) -> Transaction:
    return Transaction(
        id=uuid.uuid4(), reference="VK-7K3P9Q", amount=Decimal("52000.00"), status=status
    )


@pytest.mark.parametrize(
    ("status", "group"),
    [
        (TransactionStatus.AWAITING_PAYMENT, "unpaid"),
        (TransactionStatus.LOCKED, "paid"),
        (TransactionStatus.ESCALATED_ADMIN_REVIEW, "paid"),
        (TransactionStatus.RELEASED, "closed"),
        (TransactionStatus.RESOLVED_SPLIT, "closed"),
    ],
)
def test_state_group(status: TransactionStatus, group: str) -> None:
    assert state_group(_order(status)) == group


def test_lookup_quota_per_client() -> None:
    t0 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    for _ in range(public_order.LOOKUPS_PER_MINUTE):
        check_lookup_quota("1.2.3.4", t0)
    with pytest.raises(PublicLookupRateLimitedError):
        check_lookup_quota("1.2.3.4", t0 + timedelta(seconds=10))
    check_lookup_quota("5.6.7.8", t0)
    check_lookup_quota("1.2.3.4", t0 + timedelta(seconds=61))


def test_endpoint_returns_no_personal_data(monkeypatch) -> None:
    listing = Listing(id=uuid.uuid4(), title="iPhone 13, 128GB", price=Decimal("52000"))
    seen: list[str] = []

    def fake_find(_db, key):
        seen.append(key)
        return _order(TransactionStatus.LOCKED), listing, "sold"

    monkeypatch.setattr(router, "find_public_order", fake_find)
    response = TestClient(app).get("/public/orders/vk-7k3p9q")
    assert response.status_code == 200
    assert response.json() == {
        "reference": "VK-7K3P9Q",
        "listing_title": "iPhone 13, 128GB",
        "amount": "52000.00",
        "state_group": "paid",
        "listing_status": "sold",
    }
    assert seen == ["vk-7k3p9q"]


def test_endpoint_unknown_and_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(router, "find_public_order", lambda _db, _key: None)
    client = TestClient(app)
    assert client.get("/public/orders/VK-NOPE00").status_code == 404
    for _ in range(public_order.LOOKUPS_PER_MINUTE - 1):
        client.get("/public/orders/VK-NOPE00")
    limited = client.get("/public/orders/VK-NOPE00")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
