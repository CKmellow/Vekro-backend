import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from app.core.settings import get_settings
from app.main import app
from app.models.listing import Listing
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from app.schemas.transaction import CreateTransactionRequest
from app.services.transaction import TransactionValidationError, create_transaction
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_transaction_list import _client_as as _read_client
from tests.test_transaction_list import _listing, _order, _user
from tests.test_transaction_list import db as _db_fixture  # noqa: F401  (shared SQLite fixture)

db = _db_fixture


def _client_as(monkeypatch, db: Session, user) -> TestClient:
    """The shared client plus the CSRF cookie and header that unsafe methods need."""
    client = _read_client(monkeypatch, db, user)
    settings = get_settings()
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    client.headers[settings.csrf_header_name] = "csrf-token"
    return client


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
    open_listing = _listing(seller, "Nike Air Force 1 size 42", "6500.00")
    paid = _listing(seller, "iPhone 13, 128GB, Midnight", "52000.00")
    pending = _listing(seller, "3-seater sofa", "38000.00")
    db.add_all([open_listing, paid, pending])
    db.flush()
    db.add_all(
        [
            _order(paid, buyer, TransactionStatus.LOCKED, "VK-7K3P9Q", minutes=1),
            _order(pending, buyer, TransactionStatus.AWAITING_PAYMENT, "VK-5R2W9X", minutes=2),
        ]
    )
    db.commit()
    return {
        "seller": seller,
        "other": other,
        "buyer": buyer,
        "open": open_listing,
        "paid": paid,
        "pending": pending,
    }


def test_owner_updates_some_fields_and_keeps_the_rest(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    response = client.patch(
        f"/listings/{shop['open'].id}",
        json={"title": "  Nike Air Force 1, size 43 ", "price": "7000.00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Nike Air Force 1, size 43"
    assert body["price"] == "7000.00"
    assert body["is_serialized"] is False
    assert body["status"] == "active"


def test_update_rechecks_the_serialized_rules(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    response = client.patch(f"/listings/{shop['open'].id}", json={"is_serialized": True})

    assert response.status_code == 422
    assert "unique_id" in response.json()["detail"]


def test_update_rejects_invalid_values(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])

    assert client.patch(f"/listings/{shop['open'].id}", json={"title": " "}).status_code == 422
    assert client.patch(f"/listings/{shop['open'].id}", json={"price": "-1"}).status_code == 422


@pytest.mark.parametrize("which", ["paid", "pending"])
def test_paid_or_pending_listings_cannot_change(monkeypatch, db, shop, which) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])
    listing = shop[which]

    assert client.patch(f"/listings/{listing.id}", json={"title": "New"}).status_code == 409
    assert client.post(f"/listings/{listing.id}/archive").status_code == 409


def test_only_the_owner_can_change_a_listing(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["other"])
    listing_id = shop["open"].id

    assert client.patch(f"/listings/{listing_id}", json={"title": "Mine"}).status_code == 403
    assert client.post(f"/listings/{listing_id}/archive").status_code == 403
    assert client.post(f"/listings/{listing_id}/restore").status_code == 403


def test_buyers_and_missing_listings(monkeypatch, db, shop) -> None:
    buyer = _client_as(monkeypatch, db, shop["buyer"])
    assert buyer.post(f"/listings/{shop['open'].id}/archive").status_code == 403

    seller = _client_as(monkeypatch, db, shop["seller"])
    assert seller.post(f"/listings/{uuid.uuid4()}/archive").status_code == 404
    assert seller.patch(f"/listings/{uuid.uuid4()}", json={"title": "x y"}).status_code == 404


def test_archive_and_restore_round_trip(monkeypatch, db, shop) -> None:
    client = _client_as(monkeypatch, db, shop["seller"])
    listing_id = shop["open"].id

    archived = client.post(f"/listings/{listing_id}/archive")
    again = client.post(f"/listings/{listing_id}/archive")
    public = client.get(f"/listings/{listing_id}")
    blocked_edit = client.patch(f"/listings/{listing_id}", json={"title": "Edited"})
    listed = client.get("/listings", params={"status": "archived"}).json()
    restored = client.post(f"/listings/{listing_id}/restore")

    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert again.json()["status"] == "archived"
    assert public.json()["status"] == "archived"
    assert blocked_edit.status_code == 409
    assert [item["id"] for item in listed["items"]] == [str(listing_id)]
    assert listed["counts"]["archived"] == 1
    assert restored.json()["status"] == "active"


def test_archived_listings_take_no_payments(db, shop) -> None:
    listing: Listing = shop["open"]
    listing.archived_at = listing.created_at
    db.commit()

    with pytest.raises(TransactionValidationError, match="no longer taking payments"):
        create_transaction(
            db,
            shop["buyer"],
            CreateTransactionRequest(listing_id=listing.id, amount=Decimal("6500.00")),
        )


def test_update_and_archive_are_in_the_contract() -> None:
    paths = app.openapi()["paths"]

    assert "patch" in paths["/listings/{listing_id}"]
    assert "post" in paths["/listings/{listing_id}/archive"]
    assert "post" in paths["/listings/{listing_id}/restore"]
