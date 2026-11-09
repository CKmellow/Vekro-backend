"""#271: every money fact added in M22-M24 is anchored, the payout gate covers card-funded orders,
and the full audit checks the new global events."""

from __future__ import annotations

import json

from app.models.anchor_event import AnchorEvent
from app.models.liabilities import LiabilitiesLeaf
from app.models.payout_attempt import AttemptOutcome, PayoutAttempt
from app.models.transaction import TransactionStatus
from app.models.user import User
from app.models.ussd_session import UssdSession
from app.services import escrow_service
from app.services import transaction as transaction_service
from app.services.anchoring.batcher import run_once
from app.services.anchoring.full_audit import run_full_audit
from app.services.anchoring.liabilities import build_liabilities_snapshot
from app.services.anchoring.reserves import reconcile_reserves
from app.services.escrow_service import EscrowService, run_payout_executor_once
from app.services.manual_refunds import complete_manual_refund
from app.services.ussd import set_ussd_pin
from sqlalchemy import select

from tests.anchoring_fakes import FakeRegistry
from tests.test_anchoring_batcher import CONFIG, T0
from tests.test_card_payments import FakePesapal, _card_order, refresh_card_payment
from tests.test_liabilities import _funded, _with_tables
from tests.test_ussd import _walk

CODE = "246810"


def _anchored_actions(db, transaction_id=None) -> list[str]:
    query = select(AnchorEvent)
    if transaction_id is not None:
        query = query.where(AnchorEvent.transaction_id == transaction_id)
    return [
        json.loads(e.payload).get("action") or json.loads(e.payload)["type"]
        for e in db.execute(query.order_by(AnchorEvent.id)).scalars()
    ]


def _with_ussd_tables():
    from typing import cast

    from app.db.base import Base
    from sqlalchemy import Table

    db = _with_tables()
    Base.metadata.create_all(db.get_bind(), tables=[cast(Table, UssdSession.__table__)])
    return db


def _to_door(db, monkeypatch, transaction):
    seller = db.get(User, transaction.seller_id)
    transaction_service.dispatch_transaction(db, transaction.id, seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: CODE)
    transaction_service.mark_transaction_arrived(db, transaction.id, seller)


def test_card_funding_failure_and_mismatch_are_anchored() -> None:
    db = _with_tables()
    funded = _funded(db, "81")
    assert "callback_funding_confirmed" in _anchored_actions(db, funded.id)

    fake = FakePesapal()
    declined, _ = _card_order(db, "82", fake)
    from app.services.custody.enums import CollectionOutcome

    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")
    refresh_card_payment(db, declined, rail=fake)
    assert "card_payment_failed" in _anchored_actions(db, declined.id)

    mismatched, _ = _card_order(db, "83", fake)
    fake.status = FakePesapal.completed("1.00", reference=mismatched.reference)
    refresh_card_payment(db, mismatched, rail=fake)
    assert "card_payment_mismatch" in _anchored_actions(db, mismatched.id)


def test_manual_card_refunds_are_anchored_when_required_and_completed() -> None:
    import uuid

    db = _with_tables()
    order = _funded(db, "84")
    attempt = EscrowService(db).queue_refund_full(order, purpose=f"tx-refund:{order.id}")
    db.commit()
    assert "manual_refund_required" in _anchored_actions(db, order.id)
    complete_manual_refund(db, attempt_id=attempt.id, admin_id=uuid.uuid4(), note="Refunded.")
    assert "manual_refund_completed" in _anchored_actions(db, order.id)


def test_a_ussd_action_anchors_the_same_transition_as_the_web(monkeypatch) -> None:
    db = _with_ussd_tables()
    order = _funded(db, "85")
    buyer = db.get(User, order.buyer_id)
    buyer.phone = "+254711000885"
    db.commit()
    set_ussd_pin(db, buyer, "4321")
    _to_door(db, monkeypatch, order)
    before = _anchored_actions(db, order.id).count("transaction_transition")

    *_, done = _walk(db, buyer.phone, "1", "1", "1", CODE)

    assert done.text.startswith("END Accepted")
    transitions = [
        json.loads(e.payload)
        for e in db.execute(
            select(AnchorEvent).where(
                AnchorEvent.transaction_id == order.id,
                AnchorEvent.event_type == "transaction_transition",
            )
        ).scalars()
    ]
    assert len(transitions) == before + 1
    assert transitions[-1]["to"] == TransactionStatus.RELEASED.value
    assert transitions[-1]["actor_id"] == str(buyer.id)


def test_the_payout_gate_holds_a_card_funded_release_until_anchored(monkeypatch) -> None:
    from app.core.settings import get_settings

    gated = get_settings().model_copy(update={"anchor_gate_payouts": True})
    monkeypatch.setattr(escrow_service, "get_settings", lambda: gated)
    db = _with_tables()
    order = _funded(db, "86")
    _to_door(db, monkeypatch, order)
    transaction_service.confirm_buyer_delivery_otp(
        db, order.id, db.get(User, order.buyer_id), otp_code=CODE
    )
    chain = FakeRegistry()

    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    db.expire_all()
    attempt = db.execute(select(PayoutAttempt)).scalar_one()
    assert (attempt.outcome, attempt.failure_code) == (AttemptOutcome.UNKNOWN, "waiting_for_anchor")

    while run_once(db, chain, CONFIG, now=T0).action != "idle":
        chain.mine(2)
    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    db.expire_all()
    assert db.execute(select(PayoutAttempt)).scalar_one().outcome == AttemptOutcome.SUCCEEDED


def test_the_full_audit_checks_liabilities_and_reserves() -> None:
    db = _with_tables()
    _funded(db, "87")
    _funded(db, "88")
    snapshot = build_liabilities_snapshot(db)
    reconcile_reserves(db)
    assert {"liabilities_snapshot", "reserves_reconciliation"} <= set(_anchored_actions(db))
    assert not [f for f in run_full_audit(db).findings if f.code.startswith(("liab", "reserv"))]

    leaf = (
        db.execute(select(LiabilitiesLeaf).where(LiabilitiesLeaf.snapshot_id == snapshot.id))
        .scalars()
        .first()
    )
    leaf.amount_cents = leaf.amount_cents - 100  # quietly understate one buyer
    db.commit()
    codes = [f.code for f in run_full_audit(db).findings]
    assert "liabilities_mismatch" in codes

    db.delete(leaf)  # or drop the order altogether
    db.commit()
    assert "liabilities_mismatch" in [f.code for f in run_full_audit(db).findings]
