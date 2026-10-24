import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from app.db.base import Base
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType, SellerResolutionAction
from app.models.notification import Notification
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from app.services.transaction import submit_buyer_reconfirmation
from sqlalchemy import Table, text
from sqlalchemy.orm import Session

from tests.test_transaction_list import NOW, _client_as, _listing, _order, _user
from tests.test_transaction_list import db as _db_fixture  # noqa: F401  (shared SQLite fixture)


@pytest.fixture
def db(_db_fixture: Session) -> Session:  # noqa: F811
    column = cast(Any, Notification.__table__.c.payload)
    original = column.server_default
    column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            _db_fixture.get_bind(),
            tables=[cast(Table, Notification.__table__), cast(Table, PaymentPrompt.__table__)],
        )
    finally:
        column.server_default = original
    return _db_fixture


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _dispute(order, buyer, **fields) -> Dispute:
    base = dict(
        id=uuid.uuid4(),
        transaction_id=order.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.OPEN,
        reason="not_working",
        description="The screen flickers and goes black.",
        evidence={"internal": "never shown"},
        opened_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(fields)
    return Dispute(**base)


@pytest.fixture
def case(db: Session) -> dict[str, Any]:
    seller = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    buyer = _user(UserRole.BUYER, "Brian Otieno", "+254712345678")
    stranger = _user(UserRole.BUYER, "Someone", "+254733000111")
    db.add_all([seller, buyer, stranger])
    db.flush()
    listing = _listing(seller, "iPhone 13, 128GB, Midnight", "52000.00")
    plain = _listing(seller, "Nike Air Force 1 size 42", "6500.00")
    db.add_all([listing, plain])
    db.flush()
    order = _order(
        listing, buyer, TransactionStatus.AWAITING_BUYER_RECONFIRMATION, "VK-7K3P9Q", minutes=1
    )
    other = _order(plain, buyer, TransactionStatus.LOCKED, "VK-3M8D2L", minutes=2)
    db.add_all([order, other])
    db.flush()
    old = _dispute(order, buyer, reason="missing_parts", created_at=NOW - timedelta(days=1))
    current = _dispute(
        order,
        buyer,
        status=DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
        seller_resolution_action=SellerResolutionAction.REPAIR_SHIPPED,
        seller_resolution_notes="Replaced the screen. Sent with G4S, ref 8812.",
        evidence={"buyer_reconfirm_rejection_count": 1, "internal": "never shown"},
    )
    db.add_all([old, current])
    db.commit()
    return {"seller": seller, "buyer": buyer, "stranger": stranger, "order": order, "other": other}


@pytest.mark.parametrize("who", ["buyer", "seller"])
def test_parties_see_the_latest_dispute_without_the_evidence(monkeypatch, db, case, who) -> None:
    client = _client_as(monkeypatch, db, case[who])

    body = client.get(f"/transactions/{case['order'].id}").json()

    dispute = body["dispute"]
    assert dispute["category"] == "not_working"
    assert dispute["description"] == "The screen flickers and goes black."
    assert dispute["seller_action"] == "repair_shipped"
    assert dispute["seller_notes"] == "Replaced the screen. Sent with G4S, ref 8812."
    assert dispute["reconfirm_rejections"] == 1
    assert dispute["buyer_confirmed_resolved"] is False
    assert "evidence" not in dispute
    assert "never shown" not in str(body)


def test_orders_without_a_dispute_have_none(monkeypatch, db, case) -> None:
    client = _client_as(monkeypatch, db, case["buyer"])

    assert client.get(f"/transactions/{case['other'].id}").json()["dispute"] is None


def test_the_list_carries_the_latest_category(monkeypatch, db, case) -> None:
    client = _client_as(monkeypatch, db, case["seller"])

    items = {i["reference"]: i for i in client.get("/transactions").json()["items"]}

    assert items["VK-7K3P9Q"]["dispute_category"] == "not_working"
    assert items["VK-3M8D2L"]["dispute_category"] is None


def test_buyer_note_no_longer_overwrites_the_seller_note(db, case) -> None:
    submit_buyer_reconfirmation(
        db, case["order"].id, case["buyer"], accepted=False, notes="Still flickers."
    )

    dispute = db.query(Dispute).filter(Dispute.status != DisputeStatus.OPEN).one()
    assert dispute.seller_resolution_notes == "Replaced the screen. Sent with G4S, ref 8812."
    assert dispute.evidence["buyer_reconfirm_notes"] == "Still flickers."
