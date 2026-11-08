"""#253: refunds of card-funded orders go to a manual queue; support completes them."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payout_attempt import AttemptOutcome, PayoutAttempt
from app.services import manual_refunds
from app.services.escrow_service import (
    MANUAL_CARD_REFUND_RAIL,
    EscrowService,
    run_payout_executor_once,
)
from app.services.manual_refunds import (
    ManualRefundNotFoundError,
    ManualRefundRefusedError,
    complete_manual_refund,
    list_manual_refunds,
)
from sqlalchemy import select

from tests.test_anchoring_batcher import _db
from tests.test_card_payments import FakePesapal, _card_order, _ipn, handle_ipn
from tests.test_simulated_custody_full_flow import _seed_transaction_context

ADMIN = uuid.uuid4()


def _funded_card_order(db, suffix: str):
    fake = FakePesapal()
    transaction, prompt = _card_order(db, suffix, fake)
    assert handle_ipn(db, _ipn(transaction, prompt), rail=fake)["status"] == 200
    db.refresh(transaction)
    return transaction, prompt


def _queue_refund(db, transaction) -> PayoutAttempt:
    attempt = EscrowService(db).queue_refund_full(
        transaction, purpose=f"tx-refund:{transaction.id}"
    )
    db.commit()
    assert attempt is not None
    return attempt


def _actions(db) -> list[str]:
    return [e.action for e in db.execute(select(MoneyAuditEvent)).scalars()]


def _refund_postings(db) -> list[LedgerEntry]:
    return [
        e for e in db.execute(select(LedgerEntry)).scalars() if e.account_code == "BUYER_REFUNDABLE"
    ]


def test_a_card_refund_is_queued_for_support_and_never_sent_to_a_rail() -> None:
    db = _db()
    transaction, prompt = _funded_card_order(db, "71")

    attempt = _queue_refund(db, transaction)

    assert attempt.rail_name == MANUAL_CARD_REFUND_RAIL
    assert "manual_refund_required" in _actions(db)
    assert run_payout_executor_once(db).claimed == 0
    db.refresh(attempt)
    assert attempt.outcome == AttemptOutcome.UNKNOWN
    assert _refund_postings(db) == []
    (item,) = list_manual_refunds(db)
    assert (item.reference, item.status) == (transaction.reference, "pending")
    assert item.pesapal_order_tracking_id == prompt.checkout_request_id


def test_m_pesa_refunds_are_unchanged() -> None:
    db = _db()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="72"
    )
    attempt = _queue_refund(db, transaction)

    assert attempt.rail_name != MANUAL_CARD_REFUND_RAIL
    assert "manual_refund_required" not in _actions(db)


def test_completing_settles_once_and_is_anchored() -> None:
    db = _db()
    transaction, _prompt = _funded_card_order(db, "73")
    attempt = _queue_refund(db, transaction)

    done = complete_manual_refund(
        db,
        attempt_id=attempt.id,
        admin_id=ADMIN,
        note="Refunded in the Pesapal dashboard.",
        provider_refund_reference="PSP-REFUND-1",
    )
    again = complete_manual_refund(db, attempt_id=attempt.id, admin_id=ADMIN, note="again")

    assert done.status == again.status == "completed"
    assert len(_refund_postings(db)) == 1
    escrow = db.execute(select(Escrow).where(Escrow.transaction_id == transaction.id)).scalar_one()
    assert escrow.refunded_amount == transaction.amount
    assert _actions(db).count("manual_refund_completed") == 1
    assert list_manual_refunds(db) == []

    completed = [
        json.loads(e.payload)
        for e in db.execute(
            select(AnchorEvent).where(AnchorEvent.transaction_id == transaction.id)
        ).scalars()
        if json.loads(e.payload).get("action") == "manual_refund_completed"
    ]
    (payload,) = completed
    assert payload["actor_id"] is None and payload["actor_ref"]  # admins only as admin_ref
    assert "PSP-REFUND-1" not in json.dumps(payload)


def test_with_the_payout_gate_an_unanchored_approval_is_refused(monkeypatch) -> None:
    db = _db()
    transaction, _prompt = _funded_card_order(db, "74")
    attempt = _queue_refund(db, transaction)
    monkeypatch.setattr(
        manual_refunds, "get_settings", lambda: SimpleNamespace(anchor_gate_payouts=True)
    )

    with pytest.raises(ManualRefundRefusedError, match="not anchored"):
        complete_manual_refund(
            db,
            attempt_id=attempt.id,
            admin_id=ADMIN,
            note="Refunded in Pesapal.",
            anchor_root_of=lambda _batch_id: b"\x00" * 32,
        )
    db.refresh(attempt)
    assert attempt.outcome == AttemptOutcome.UNKNOWN
    assert _refund_postings(db) == []


def test_only_manual_refund_attempts_can_be_completed() -> None:
    db = _db()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="75"
    )
    attempt = _queue_refund(db, transaction)

    with pytest.raises(ManualRefundNotFoundError):
        complete_manual_refund(db, attempt_id=attempt.id, admin_id=ADMIN, note="nope")
    with pytest.raises(ManualRefundNotFoundError):
        complete_manual_refund(db, attempt_id=uuid.uuid4(), admin_id=ADMIN, note="nope")
