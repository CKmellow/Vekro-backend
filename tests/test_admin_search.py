# ruff: noqa: F811 - pytest fixtures are imported from the dispute summary tests and injected by name.
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from sqlalchemy.orm import Session

from tests.test_dispute_summary import _clear_overrides, db  # noqa: F401  (fixtures)
from tests.test_transaction_list import _client_as, _listing, _order, _user
from tests.test_transaction_list import db as _db_fixture  # noqa: F401


@pytest.fixture
def shop(db: Session) -> dict[str, Any]:
    admin = _user(UserRole.ADMIN, "Vekro Support", "+254700000001")
    amina = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    juma = _user(UserRole.SELLER, "Juma Shoes", "+254733445566")
    brian = _user(UserRole.BUYER, "Brian Otieno", "+254712345678")
    db.add_all([admin, amina, juma, brian])
    db.flush()
    phone = _listing(amina, "iPhone 13, 128GB, Midnight", "52000.00")
    phone.is_serialized = True
    phone.unique_id = "352098114371234"
    shoes = _listing(juma, "Nike Air Force 1 size 42", "6500.00")
    db.add_all([phone, shoes])
    db.flush()
    paid = _order(phone, brian, TransactionStatus.LOCKED, "VK-7K3P9Q", minutes=10)
    done = _order(shoes, brian, TransactionStatus.RELEASED, "VK-3M8D2L", minutes=20)
    done.created_at = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)  # 2 Oct 01:30 in Nairobi
    db.add_all([paid, done])
    db.flush()
    db.add(
        Notification(
            id=uuid.uuid4(),
            user_id=amina.id,
            transaction_id=paid.id,
            event_type=NotificationEventType.TRANSACTION_LOCKED,
            title="Buyer payment confirmed",
            message="Paid.",
            payload={},
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db.commit()
    return {"admin": admin, "amina": amina}


def _refs(client, **params) -> list[str]:
    return [
        i["reference"] for i in client.get("/admin/transactions", params=params).json()["items"]
    ]


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("vk-7k3", ["VK-7K3P9Q"]),
        ("0712 345 678", ["VK-3M8D2L", "VK-7K3P9Q"]),
        ("+254733445566", ["VK-3M8D2L"]),
        ("733445566", ["VK-3M8D2L"]),
        ("amina", ["VK-7K3P9Q"]),
        ("1234", ["VK-7K3P9Q"]),
        ("nike", ["VK-3M8D2L"]),
        ("nothing", []),
    ],
)
def test_search_matches_reference_phone_seller_serial_and_title(
    monkeypatch, db, shop, q, expected
) -> None:
    client = _client_as(monkeypatch, db, shop["admin"])

    assert sorted(_refs(client, q=q)) == sorted(expected)


def test_filters_and_recent_activity(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["admin"])

    assert _refs(client) == ["VK-7K3P9Q", "VK-3M8D2L"]
    assert _refs(client, status="released") == ["VK-3M8D2L"]
    assert _refs(client, serialized="true") == ["VK-7K3P9Q"]
    assert _refs(client, min_amount="10000") == ["VK-7K3P9Q"]
    assert _refs(client, max_amount="10000") == ["VK-3M8D2L"]
    assert _refs(client, placed_from="2026-10-02", placed_to="2026-10-02") == ["VK-3M8D2L"]
    assert _refs(client, placed_to="2026-10-01") == []


def test_rows_carry_full_phones_and_the_last_event(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["admin"])

    item = client.get("/admin/transactions", params={"q": "VK-7K3P9Q"}).json()["items"][0]

    assert item["buyer"] == {"name": "Brian Otieno", "phone": "+254712345678"}
    assert item["seller"]["phone"] == "+254722118904"
    assert item["last_event"]["title"] == "Buyer payment confirmed"
    assert item["dispute_id"] is None


def test_search_is_admin_only(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["amina"])

    assert client.get("/admin/transactions").status_code == 403
