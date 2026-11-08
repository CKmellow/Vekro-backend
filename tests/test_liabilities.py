"""#261: the liabilities snapshot, its anchored total and each buyer's proof."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.db.base import Base
from app.models.anchor_event import AnchorEvent
from app.models.liabilities import LiabilitiesLeaf, LiabilitiesSnapshot
from app.models.user import UserRole
from app.routers.verification import get_liabilities_proof
from app.services.anchoring.liabilities import build_liabilities_snapshot, liabilities_proof
from app.services.anchoring.sum_tree import SumNode, SumProofStep, order_commit, verify_sum_proof
from fastapi import HTTPException
from sqlalchemy import Table, select

from tests.test_anchoring_batcher import _db
from tests.test_card_payments import FakePesapal, _card_order, _ipn, handle_ipn


def _with_tables():
    db = _db()
    Base.metadata.create_all(
        db.get_bind(),
        tables=[
            cast(Table, LiabilitiesSnapshot.__table__),
            cast(Table, LiabilitiesLeaf.__table__),
        ],
    )
    return db


_FAKE = FakePesapal()  # one fake: Pesapal's tracking ids stay unique across orders


def _funded(db, suffix: str):
    fake = _FAKE
    transaction, prompt = _card_order(db, suffix, fake)
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    db.refresh(transaction)
    return transaction


def _b(text: str) -> bytes:
    return bytes.fromhex(text.removeprefix("0x"))


def _check(proof: dict[str, Any]) -> bool:
    return verify_sum_proof(
        salt=_b(proof["leaf"]["salt"]),
        commit=_b(proof["leaf"]["order_commit"]),
        cents=int(proof["leaf"]["amount_cents"]),
        proof=[
            SumProofStep(SumNode(_b(s["sibling_hash"]), int(s["sibling_sum"])), s["side"])
            for s in proof["proof"]
        ],
        root=SumNode(_b(proof["root"]["hash"]), int(proof["root"]["sum"])),
    )


def test_a_snapshot_counts_every_held_order_and_anchors_only_the_total() -> None:
    db = _with_tables()
    first, second = _funded(db, "91"), _funded(db, "92")
    _card_order(db, "93", _FAKE)  # unpaid: nothing held

    snapshot = build_liabilities_snapshot(db)

    assert snapshot.leaf_count == 2
    assert int(snapshot.total_cents) == int((first.amount + second.amount) * 100)
    event = db.get(AnchorEvent, snapshot.anchor_event_id)
    assert event is not None and event.transaction_id is None
    payload = json.loads(event.payload)
    assert payload["type"] == "liabilities_snapshot"
    assert payload["root"] == "0x" + snapshot.root_hash.hex()
    assert payload["total"] == f"{(first.amount + second.amount):.2f}"
    assert payload["leaf_count"] == 2
    assert str(first.id) not in event.payload and str(second.id) not in event.payload


def test_each_buyer_proves_their_amount_is_in_the_anchored_total() -> None:
    db = _with_tables()
    orders = [_funded(db, "94"), _funded(db, "95"), _funded(db, "96")]
    build_liabilities_snapshot(db)

    for order in orders:
        proof = liabilities_proof(db, order.id)
        assert proof is not None
        assert _check(proof)
        assert proof["leaf"]["order_commit"] == "0x" + order_commit(order.id).hex()
        assert int(proof["leaf"]["amount_cents"]) == int(order.amount * 100)
        anchored = json.loads(proof["anchor"]["payload"])
        assert anchored["root"] == proof["root"]["hash"]
        assert anchored["total"] == proof["total"]


def test_only_the_latest_snapshot_counts() -> None:
    db = _with_tables()
    order = _funded(db, "97")
    build_liabilities_snapshot(db)
    assert liabilities_proof(db, order.id) is not None

    from app.models.escrow import Escrow

    escrow = db.execute(select(Escrow).where(Escrow.transaction_id == order.id)).scalar_one()
    escrow.released_amount = escrow.funded_amount
    db.commit()
    later = build_liabilities_snapshot(db)

    assert later.leaf_count == 0 and later.root_hash is None and int(later.total_cents) == 0
    assert json.loads(db.get(AnchorEvent, later.anchor_event_id).payload)["root"] is None
    assert liabilities_proof(db, order.id) is None


def test_only_the_buyer_or_an_admin_gets_the_proof() -> None:
    db = _with_tables()
    order = _funded(db, "98")
    build_liabilities_snapshot(db)

    def as_user(user_id: uuid.UUID, role: UserRole):
        return get_liabilities_proof(
            order.id, SimpleNamespace(id=user_id, role=role), db  # type: ignore[arg-type]
        )

    assert as_user(order.buyer_id, UserRole.BUYER).snapshot_id
    assert as_user(uuid.uuid4(), UserRole.ADMIN).snapshot_id
    for user_id, role in ((order.seller_id, UserRole.SELLER), (uuid.uuid4(), UserRole.BUYER)):
        with pytest.raises(HTTPException) as refused:
            as_user(user_id, role)
        assert refused.value.status_code == 404
