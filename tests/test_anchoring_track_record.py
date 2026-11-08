"""M21 A9 (#231): a seller's completed sales as public, anchored events anyone can count against
the AnchorRegistry, with no buyer or order data."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.settings import get_settings
from app.db.session import get_db
from app.main import app
from app.models.anchor_event import AnchorEvent
from app.models.transaction import Transaction, TransactionStatus
from app.services import transaction as transaction_service
from app.services.anchoring import payloads
from app.services.anchoring.batcher import run_once
from app.services.anchoring.full_audit import run_full_audit
from app.services.anchoring.merkle import ProofStep, verify_inclusion
from app.services.anchoring.recorder import SELLER_SALES_SOURCE
from app.services.anchoring.track_record import backfill_seller_sales, seller_track_record
from app.services.transaction import (
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_transaction_arrived,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tests.anchoring_fakes import FakeRegistry
from tests.test_anchoring_batcher import CONFIG, T0, _db
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _seed_transaction_context,
)


def _released(db: Session, monkeypatch, suffix: str) -> Transaction:
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "135790")
    mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
    confirm_buyer_delivery_otp(db, transaction_id=transaction.id, buyer=buyer, otp_code="135790")
    assert transaction.status == TransactionStatus.RELEASED
    return transaction


def _anchor_everything(db: Session, chain: FakeRegistry) -> None:
    while run_once(db, chain, CONFIG, now=T0).action != "idle":
        chain.mine(2)


def _sale_events(db: Session) -> list[AnchorEvent]:
    return list(
        db.execute(
            select(AnchorEvent).where(AnchorEvent.source_table == SELLER_SALES_SOURCE)
        ).scalars()
    )


def _verified_count(record: dict[str, Any], chain: FakeRegistry) -> int:
    """What a browser verifier does: count proofs that reach the registry root for this seller."""
    from app.services.anchoring.canonical import leaf_hash

    count = 0
    for sale in record["sales"]:
        body = json.loads(sale["payload"])
        anchor = sale["anchor"]
        leaf = leaf_hash(
            bytes.fromhex(sale["salt"][2:]),
            bytes.fromhex(sale["prev_event_hash"][2:]),
            sale["payload"],
        )
        if anchor is None or leaf.hex() != sale["leaf_hash"][2:]:
            continue
        proof = [ProofStep(bytes.fromhex(p["sibling"][2:]), p["side"]) for p in anchor["proof"]]
        on_chain = chain.root_of(anchor["batch_id"])
        if body["seller_id"] == record["seller_id"] and verify_inclusion(
            leaf, proof, on_chain, index=anchor["leaf_index"], size=anchor["leaf_count"]
        ):
            count += 1
    return count


def test_a_completed_sale_records_one_public_event_without_buyer_data(monkeypatch) -> None:
    db = _db()
    transaction = _released(db, monkeypatch, "81")
    [event] = _sale_events(db)
    body = json.loads(event.payload)
    assert event.transaction_id is None  # the global chain, not the order's
    assert body == {
        "v": 1,
        "type": "seller_sale_completed",
        "transaction_id": None,
        "at": body["at"],
        "seller_id": str(transaction.seller_id),
        "sale_ref": payloads.sale_ref(transaction.id),
        "outcome": "released",
        "amount_band": payloads.amount_band(transaction.amount),
    }
    for private in (str(transaction.id), str(transaction.buyer_id), transaction.reference):
        assert private not in event.payload


def test_the_track_record_verifies_each_sale_against_the_registry(monkeypatch) -> None:
    db = _db()
    first = _released(db, monkeypatch, "82")
    chain = FakeRegistry()
    _anchor_everything(db, chain)

    # A second sale by another seller, and an unpaid order, never count for this seller.
    _released(db, monkeypatch, "83")
    record = json.loads(
        json.dumps(seller_track_record(db, first.seller_id, get_settings()), default=str)
    )
    assert (record["completed_sales"], record["recorded_sales"]) == (1, 1)
    assert _verified_count(record, chain) == 1
    assert run_full_audit(db, chain.root_of).verdict == "CLEAN"


def test_a_sale_waiting_for_its_anchor_is_not_counted_as_verified(monkeypatch) -> None:
    db = _db()
    transaction = _released(db, monkeypatch, "84")
    chain = FakeRegistry()
    record = json.loads(
        json.dumps(seller_track_record(db, transaction.seller_id, get_settings()), default=str)
    )
    assert record["recorded_sales"] == 1
    assert record["sales"][0]["anchor"] is None
    assert _verified_count(record, chain) == 0


def test_the_full_audit_catches_a_forged_or_missing_sale(monkeypatch) -> None:
    db = _db()
    transaction = _released(db, monkeypatch, "85")
    chain = FakeRegistry()
    _anchor_everything(db, chain)

    # The order is moved to another status behind the log's back: its sale no longer holds.
    db.execute(
        update(Transaction)
        .where(Transaction.id == transaction.id)
        .values(status=TransactionStatus.REFUNDED_BUYER)
    )
    db.commit()
    codes = {finding.code for finding in run_full_audit(db, chain.root_of).findings}
    assert "sale_record_mismatch" in codes


def test_the_backfill_records_sales_from_before_a9_once(monkeypatch) -> None:
    from app.services import transitions

    db = _db()
    # A sale completed before A9 existed: no track-record event was written.
    monkeypatch.setattr(transitions, "record_seller_sale", lambda *_args, **_kwargs: None)
    transaction = _released(db, monkeypatch, "86")
    monkeypatch.undo()
    assert _sale_events(db) == []
    codes = {finding.code for finding in run_full_audit(db).findings}
    assert codes == {"sale_event_missing"}

    assert backfill_seller_sales(db) == 1
    assert backfill_seller_sales(db) == 0
    [event] = _sale_events(db)
    assert json.loads(event.payload)["backfilled"] is True
    assert json.loads(event.payload)["seller_id"] == str(transaction.seller_id)
    assert run_full_audit(db).verdict == "CLEAN"


@pytest.fixture
def api_db(monkeypatch) -> Iterator[tuple[Session, Transaction]]:
    db = _db()
    transaction = _released(db, monkeypatch, "87")

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        yield db, transaction
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_the_public_endpoint_serves_the_record_and_404s_non_sellers(api_db) -> None:
    from app.routers.verification import get_seller_track_record
    from fastapi import HTTPException
    from starlette.requests import Request

    db, transaction = api_db
    request = Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1)})
    response = get_seller_track_record(transaction.seller_id, request, db)
    assert response.completed_sales == 1 and len(response.sales) == 1
    with pytest.raises(HTTPException) as error:
        get_seller_track_record(transaction.buyer_id, request, db)
    assert error.value.status_code == 404
