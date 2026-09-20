import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from app.main import app
from app.models.listing import Listing
from app.routers import listings as listings_router
from app.services.listing import ListingNotFoundError, get_listing_by_id
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self, listing: Listing | None = None) -> None:
        self.listing = listing

    def get(self, _model: Any, listing_id):
        if self.listing is not None and listing_id == self.listing.id:
            return self.listing
        return None


def _build_listing() -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        title="Serialized Watch",
        price=Decimal("9999.00"),
        is_serialized=True,
        unique_id="WATCH-1234",
        dispute_policy={"resolution": "seller_review", "window_hours": 24},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_get_listing_returns_listing_by_id(monkeypatch) -> None:
    listing = _build_listing()

    def fake_get_listing_by_id(_db, listing_id):
        assert listing_id == listing.id
        return listing

    monkeypatch.setattr(listings_router, "get_listing_by_id", fake_get_listing_by_id)

    client = TestClient(app)
    response = client.get(f"/listings/{listing.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(listing.id)
    assert body["is_serialized"] is True
    assert body["unique_id"] == "WATCH-1234"
    assert body["dispute_policy"]["resolution"] == "seller_review"


def test_get_listing_not_found_returns_consistent_404(monkeypatch) -> None:
    def fake_get_listing_by_id(_db, _listing_id):
        raise ListingNotFoundError("Listing not found.")

    monkeypatch.setattr(listings_router, "get_listing_by_id", fake_get_listing_by_id)

    client = TestClient(app)
    missing_id = uuid.uuid4()
    response = client.get(f"/listings/{missing_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Listing not found."


def test_get_listing_service_returns_listing_when_present() -> None:
    listing = _build_listing()
    db = _FakeDb(listing=listing)

    fetched = get_listing_by_id(cast(Session, db), listing.id)

    assert fetched is listing


def test_get_listing_service_raises_not_found_when_missing() -> None:
    db = _FakeDb(listing=None)

    with pytest.raises(ListingNotFoundError, match="Listing not found."):
        get_listing_by_id(cast(Session, db), uuid.uuid4())
