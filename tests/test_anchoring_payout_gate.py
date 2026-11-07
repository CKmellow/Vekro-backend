"""M21 A8 (#230): no payout reaches a rail until its authorisation is anchored and its proof matches
the registry root. A payout that disagrees with what was anchored halts instead of paying."""

from typing import Any

import pytest
from app.core.settings import get_settings
from app.models.collection_attempt import AttemptOutcome
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionStatus
from app.services import escrow_service
from app.services import transaction as transaction_service
from app.services.anchoring.batcher import run_once
from app.services.anchoring.chain import ChainUnavailableError
from app.services.anchoring.payout_gate import destination_ref
from app.services.anchoring.status import anchoring_status
from app.services.escrow_service import run_payout_executor_once
from app.services.transaction import (
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_transaction_arrived,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tests.anchoring_fakes import FakeRegistry
from tests.test_anchoring_batcher import CONFIG, T0, _db, _settings
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _seed_transaction_context,
)


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    gated = get_settings().model_copy(update={"anchor_gate_payouts": True})
    monkeypatch.setattr(escrow_service, "get_settings", lambda: gated)


def _released_order(db: Session, monkeypatch: pytest.MonkeyPatch, suffix: str) -> Transaction:
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "246810")
    mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
    confirm_buyer_delivery_otp(db, transaction_id=transaction.id, buyer=buyer, otp_code="246810")
    assert transaction.status == TransactionStatus.RELEASED
    return transaction


def _attempt(db: Session) -> PayoutAttempt:
    db.expire_all()
    return db.execute(select(PayoutAttempt)).scalar_one()


def _anchor_everything(db: Session, chain: FakeRegistry) -> None:
    while run_once(db, chain, CONFIG, now=T0).action != "idle":
        chain.mine(2)


def _releases(db: Session) -> list[Any]:
    return list(
        db.execute(select(LedgerEntry).where(LedgerEntry.account_code == "SELLER_PAYABLE"))
        .scalars()
        .all()
    )


def _halt_audits(db: Session) -> list[MoneyAuditEvent]:
    return list(
        db.execute(
            select(MoneyAuditEvent).where(MoneyAuditEvent.action.like("%halted_anchor_mismatch"))
        ).scalars()
    )


def test_without_the_gate_payouts_run_as_before(monkeypatch) -> None:
    db = _db()
    _released_order(db, monkeypatch, "70")
    run_payout_executor_once(db)
    assert _attempt(db).outcome == AttemptOutcome.SUCCEEDED


def test_the_queued_authorisation_binds_the_destination_without_the_phone(monkeypatch) -> None:
    db = _db()
    _released_order(db, monkeypatch, "71")
    queued = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "payout_release_queued")
    ).scalar_one()
    phone = _attempt(db).request_snapshot["destination_phone"]
    assert queued.details["destination_ref"] == destination_ref(phone)
    assert phone not in str(queued.details)


def test_a_payout_waits_for_its_anchor_then_pays(monkeypatch, gate_on) -> None:
    db = _db()
    _released_order(db, monkeypatch, "72")
    chain = FakeRegistry()

    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    waiting = _attempt(db)
    assert (waiting.outcome, waiting.failure_code) == (AttemptOutcome.UNKNOWN, "waiting_for_anchor")
    assert _releases(db) == []
    status = anchoring_status(db, _settings(anchor_gate_payouts=False))
    assert status.payouts_waiting_for_anchor == 1
    assert status.oldest_payout_waiting_since is not None

    # Batched but not yet confirmed: still waiting.
    assert run_once(db, chain, CONFIG, now=T0).action == "submitted"
    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    assert _attempt(db).failure_code == "waiting_for_anchor"

    chain.mine(2)
    assert run_once(db, chain, CONFIG, now=T0).action == "confirmed"
    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    assert _attempt(db).outcome == AttemptOutcome.SUCCEEDED
    assert len(_releases(db)) == 1


def test_an_unreadable_registry_holds_the_payout_without_failing_it(monkeypatch, gate_on) -> None:
    db = _db()
    _released_order(db, monkeypatch, "73")
    chain = FakeRegistry()
    _anchor_everything(db, chain)

    def down(_batch: int) -> bytes:
        raise ChainUnavailableError("RPC unreachable")

    run_payout_executor_once(db, anchor_root_of=down)
    attempt = _attempt(db)
    assert (attempt.outcome, attempt.failure_code) == (
        AttemptOutcome.UNKNOWN,
        "anchor_check_unavailable",
    )
    run_payout_executor_once(db, anchor_root_of=chain.root_of)
    assert _attempt(db).outcome == AttemptOutcome.SUCCEEDED


def test_a_reorged_authorisation_waits_for_the_batcher(monkeypatch, gate_on) -> None:
    db = _db()
    _released_order(db, monkeypatch, "74")
    _anchor_everything(db, FakeRegistry())
    run_payout_executor_once(db, anchor_root_of=lambda _batch: bytes(32))
    assert _attempt(db).failure_code == "waiting_for_anchor"


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ({"amount": "9999.00"}, "differs from its anchored authorisation"),
        ({"destination_phone": "+254799999999"}, "destination differs"),
    ],
)
def test_a_payout_edited_after_its_anchor_halts_and_never_pays(
    monkeypatch, gate_on, tamper: dict[str, str], reason: str
) -> None:
    db = _db()
    _released_order(db, monkeypatch, "75")
    chain = FakeRegistry()
    _anchor_everything(db, chain)

    attempt = _attempt(db)
    if "amount" in tamper:
        values: dict[str, Any] = {"amount": tamper["amount"]}
    else:
        values = {"request_snapshot": {**attempt.request_snapshot, **tamper}}
    db.execute(update(PayoutAttempt).where(PayoutAttempt.id == attempt.id).values(**values))
    db.commit()

    for _ in range(2):
        run_payout_executor_once(db, anchor_root_of=chain.root_of)
    halted = _attempt(db)
    assert (halted.outcome, halted.failure_code) == (
        AttemptOutcome.UNKNOWN,
        "anchor_mismatch_halted",
    )
    assert reason in (halted.failure_reason or "")
    assert _releases(db) == []
    assert len(_halt_audits(db)) == 1  # audited once, not on every run
    status = anchoring_status(db, _settings())
    assert (status.payouts_halted, status.payouts_waiting_for_anchor) == (1, 0)


def test_a_root_that_differs_on_chain_halts_the_payout(monkeypatch, gate_on) -> None:
    db = _db()
    _released_order(db, monkeypatch, "76")
    _anchor_everything(db, FakeRegistry())
    run_payout_executor_once(db, anchor_root_of=lambda _batch: b"\x0b" * 32)
    assert _attempt(db).failure_code == "anchor_mismatch_halted"
    assert _releases(db) == []


def test_the_gate_needs_anchoring_and_production_needs_the_gate() -> None:
    with pytest.raises(ValueError, match="ANCHOR_GATE_PAYOUTS=true needs ANCHOR_ENABLED"):
        _settings(anchor_gate_payouts=True)
    with pytest.raises(ValueError, match="ANCHOR_GATE_PAYOUTS=true is required"):
        _settings(
            environment="production",
            anchor_enabled=True,
            anchor_rpc_url="https://sepolia.base.org",
            anchor_chain_id=84532,
            anchor_registry_address="0x" + "ab" * 20,
            anchor_private_key="0x" + "12" * 32,
            anchor_admin_ref_secret="x" * 40,
        )
