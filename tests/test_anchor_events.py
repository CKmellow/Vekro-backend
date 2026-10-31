"""M21 A3: the anchor event log. Every money fact gets an event in its own DB transaction, events
form per-transaction hash chains, source-backed payloads rebuild from their rows, the money history
tables are append-only, and nothing reaches the log without its hook."""

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.settings import Settings, get_settings
from app.models.anchor_event import AnchorEvent
from app.models.dispute import AdminDecision, Dispute
from app.models.ledger_entry import LedgerEntry, LedgerEntrySide
from app.models.money_audit_event import MoneyAuditEvent
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.services import transaction as transaction_service
from app.services.anchoring import payloads
from app.services.anchoring.admin_ref import admin_ref, resolve_admin_ref
from app.services.anchoring.backfill import BackfillRefusedError, backfill_anchor_events
from app.services.anchoring.canonical import ZERO_HASH, canonical_json, leaf_hash
from app.services.anchoring.recorder import AnchorCompletenessError
from app.services.audit import ACTOR_PROVIDER, record_money_audit_event
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from app.services.dispute import force_resolve_dispute_case
from app.services.dispute_escalation import escalate_dispute
from app.services.escrow_service import EscrowService
from app.services.ledger import EscrowLedger, LedgerMovement
from app.services.transaction import (
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_transaction_arrived,
    report_functional_issue,
    withhold_buyer_delivery_otp,
)
from app.services.transitions import transition_transaction
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from tests.test_return_case_timeouts import _db_with_audit
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _open_and_fund_escrow,
    _seed_transaction_context,
)

APP = Path(__file__).resolve().parent.parent / "app"


def _events(db: Session, transaction_id: uuid.UUID | None) -> list[AnchorEvent]:
    condition = (
        AnchorEvent.transaction_id.is_(None)
        if transaction_id is None
        else AnchorEvent.transaction_id == transaction_id
    )
    return list(db.execute(select(AnchorEvent).where(condition).order_by(AnchorEvent.id)).scalars())


def _assert_chain(events: list[AnchorEvent]) -> None:
    prev = ZERO_HASH
    for event in events:
        assert bytes(event.prev_event_hash) == prev, event.event_type
        assert canonical_json(json.loads(event.payload)) == event.payload
        assert bytes(event.leaf_hash) == leaf_hash(bytes(event.salt), prev, event.payload)
        prev = bytes(event.leaf_hash)


def _last_status(events: list[AnchorEvent]) -> str:
    for event in reversed(events):
        body = json.loads(event.payload)
        if event.event_type == payloads.TRANSACTION_TRANSITION:
            return body["to"]
        if event.event_type == payloads.TRANSACTION_CREATED:
            return "awaiting_payment"
    raise AssertionError("no status event")


def _admin(db: Session) -> User:
    admin = User(
        id=uuid.uuid4(),
        name="Support",
        phone=f"+2547{uuid.uuid4().int % 10**8:08d}",
        role=UserRole.ADMIN,
        password_hash="x",
    )
    db.add(admin)
    db.commit()
    return admin


def _funded_dispute_case(db: Session, monkeypatch, suffix: str):
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=True, suffix=suffix
    )
    provider = SimulatedCustodyProvider(
        db, collection_rail=SimulatedRail(), payout_rail=SimulatedRail()
    )
    _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "111222")
    mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
    confirm_buyer_delivery_otp(db, transaction_id=transaction.id, buyer=buyer, otp_code="111222")
    report_functional_issue(
        db,
        transaction_id=transaction.id,
        buyer=buyer,
        category="not_working",
        description="Screen flickers after a day — secret detail 0712345678",
    )
    escalate_dispute(db, transaction.id, buyer, reason="Seller stopped answering, call 0712345678")
    return buyer, seller, transaction


def test_a_full_dispute_case_is_one_valid_chain_ending_in_the_current_status(monkeypatch) -> None:
    db = _db_with_audit()
    admin = _admin(db)
    _buyer, _seller, transaction = _funded_dispute_case(db, monkeypatch, "81")
    dispute = db.execute(select(Dispute)).scalar_one()

    force_resolve_dispute_case(
        db,
        dispute.id,
        decision=AdminDecision.SPLIT,
        reason="Both are partly right.",
        actor_id=admin.id,
        seller_share=Decimal("0.6"),
    )

    events = _events(db, transaction.id)
    _assert_chain(events)
    types = [event.event_type for event in events]
    assert types[0] == payloads.TRANSACTION_CREATED
    assert types.count(payloads.LEDGER_ENTRY) == 2  # simulated funding, debit and credit
    assert {payloads.DISPUTE_OPENED, payloads.DISPUTE_ESCALATED, payloads.ADMIN_DECISION} <= set(
        types
    )
    transitions = [
        json.loads(e.payload) for e in events if e.event_type == payloads.TRANSACTION_TRANSITION
    ]
    assert [(t["from"], t["to"]) for t in transitions] == [
        ("awaiting_payment", "locked"),
        ("locked", "out_for_delivery"),
        ("out_for_delivery", "at_door_pending_inspection"),
        ("at_door_pending_inspection", "hold_24h"),
        ("hold_24h", "disputed_functional"),
        ("disputed_functional", "escalated_admin_review"),
        ("escalated_admin_review", "resolved_split"),
    ]
    assert _last_status(events) == transaction.status.value == "resolved_split"

    decision = next(
        json.loads(e.payload) for e in events if e.event_type == payloads.ADMIN_DECISION
    )
    assert decision["release_amount"] == "1440.00" and decision["refund_amount"] == "960.00"
    assert decision["seller_share"] == "0.6000"
    # D5: admins appear only as an HMAC ref, never by UUID, anywhere in the chain.
    assert decision["admin_ref"] == admin_ref(admin.id)
    assert all(str(admin.id) not in e.payload for e in events)
    assert resolve_admin_ref(decision["admin_ref"], [uuid.uuid4(), admin.id]) == admin.id
    # D4: party free text is bound by hash, never copied.
    assert all("0712345678" not in e.payload for e in events)
    opened = next(json.loads(e.payload) for e in events if e.event_type == "dispute_opened")
    assert opened["description_keccak"] == payloads.free_text(dispute.description)


def test_the_three_step_door_rejection_is_three_chained_transitions(monkeypatch) -> None:
    db = _db_with_audit()
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="82"
    )
    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "111222")
    mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
    withhold_buyer_delivery_otp(db, transaction_id=transaction.id, buyer=buyer, reason="damaged")

    events = _events(db, transaction.id)
    _assert_chain(events)
    tail = [
        json.loads(e.payload) for e in events if e.event_type == payloads.TRANSACTION_TRANSITION
    ][-3:]
    assert [t["to"] for t in tail] == ["return_in_transit", "returned_to_seller", "refunded_buyer"]
    assert {t["door_rejection_reason"] for t in tail} == {"damaged"}
    assert {t["actor_id"] for t in tail} == {str(buyer.id)}
    assert _last_status(events) == transaction.status.value


def test_source_backed_payloads_rebuild_from_their_rows(monkeypatch) -> None:
    db = _db_with_audit()
    _buyer, _seller, transaction = _funded_dispute_case(db, monkeypatch, "83")
    events = _events(db, transaction.id)

    rebuilt = 0
    for event in events:
        if event.source_table == "ledger_entries":
            entry = db.get(LedgerEntry, uuid.UUID(event.source_id))
            payload = payloads.ledger_entry_payload(entry, transaction.id)
        elif event.source_table == "money_audit_events":
            audit = db.get(MoneyAuditEvent, uuid.UUID(event.source_id))
            payload = payloads.money_audit_payload(audit)
        elif event.source_table == "transactions":
            payload = payloads.transaction_created_payload(db.get(Transaction, transaction.id))
        else:
            continue
        assert canonical_json(payload) == event.payload
        rebuilt += 1
    assert rebuilt >= 3


def _one_anchor_event(db: Session) -> AnchorEvent:
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="84"
    )
    return _events(db, transaction.id)[0]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount = amount + 1",
        "DELETE FROM ledger_entries",
        "UPDATE money_audit_events SET reason = 'edited'",
        "DELETE FROM money_audit_events",
        "UPDATE anchor_events SET payload = '{}'",
        "DELETE FROM anchor_events",
    ],
)
def test_money_history_tables_are_append_only(monkeypatch, statement) -> None:
    db = _db_with_audit()
    _funded_dispute_case(db, monkeypatch, "85")
    record_money_audit_event(db, action="probe", actor_type="system")
    db.commit()
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(text(statement))
    db.rollback()


def test_the_batcher_may_set_seq_batch_and_index_once_and_nothing_else() -> None:
    db = _db_with_audit()
    event = _one_anchor_event(db)
    db.execute(
        text("UPDATE anchor_events SET seq = 1, batch_id = 1, leaf_index = 0 WHERE id = :id"),
        {"id": event.id},
    )
    db.commit()
    for statement in (
        "UPDATE anchor_events SET seq = 2, batch_id = 2, leaf_index = 0 WHERE id = :id",
        "UPDATE anchor_events SET seq = NULL, batch_id = NULL, leaf_index = NULL WHERE id = :id",
    ):
        with pytest.raises(DatabaseError, match="append-only"):
            db.execute(text(statement), {"id": event.id})
        db.rollback()


def test_batch_assignment_cannot_carry_a_payload_edit() -> None:
    db = _db_with_audit()
    event = _one_anchor_event(db)
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(
            text(
                "UPDATE anchor_events SET seq = 1, batch_id = 1, leaf_index = 0, payload = '{}' "
                "WHERE id = :id"
            ),
            {"id": event.id},
        )


def test_strict_mode_refuses_money_facts_without_events() -> None:
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="86"
    )

    transaction.status = TransactionStatus.LOCKED  # bypassing transition_transaction
    with pytest.raises(AnchorCompletenessError, match="without a transition event"):
        db.flush()
    db.rollback()

    db.add(
        LedgerEntry(
            id=uuid.uuid4(),
            escrow_id=uuid.uuid4(),
            entry_group_id=uuid.uuid4(),
            idempotency_key="bypass",
            account_code="ESCROW_HELD",
            side=LedgerEntrySide.DEBIT,
            amount=Decimal("1.00"),
            created_at=datetime.now(UTC),
        )
    )
    with pytest.raises(AnchorCompletenessError, match="ledger entry"):
        db.flush()
    db.rollback()

    db.add(MoneyAuditEvent(id=uuid.uuid4(), actor_type="system", action="x", correlation_id="c"))
    with pytest.raises(AnchorCompletenessError, match="money audit event"):
        db.flush()


def test_outside_strict_mode_a_missed_hook_is_logged_and_recorded(monkeypatch, caplog) -> None:
    lenient = get_settings().model_copy(update={"anchor_strict_events": False})
    monkeypatch.setattr("app.core.settings.get_settings", lambda: lenient)
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="87"
    )

    with caplog.at_level(logging.ERROR, logger="app.anchoring"):
        transaction.status = TransactionStatus.LOCKED
        db.commit()

    assert "anchor_event_missing" in caplog.text
    events = _events(db, transaction.id)
    _assert_chain(events)
    fallback = json.loads(events[-1].payload)
    assert (fallback["from"], fallback["to"]) == ("awaiting_payment", "locked")
    assert (fallback["actor_type"], fallback["reason"]) == ("unknown", "unrecorded_transition")


def test_money_audit_rows_without_a_transaction_join_the_global_chain() -> None:
    db = _db_with_audit()
    record_money_audit_event(db, action="first", actor_type="system")
    db.commit()
    record_money_audit_event(db, action="second", actor_type="system")
    db.commit()
    events = _events(db, None)
    assert [json.loads(e.payload)["action"] for e in events] == ["first", "second"]
    _assert_chain(events)


def test_a_global_funding_event_matched_to_an_escrow_is_linked_into_its_chain() -> None:
    db = _db_with_audit()
    buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="88"
    )
    provider = SimulatedCustodyProvider(
        db, collection_rail=SimulatedRail(), payout_rail=SimulatedRail()
    )
    _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)
    reference = db.execute(text("SELECT provider_reference FROM collection_attempts")).scalar_one()

    record_money_audit_event(
        db,
        action="funding_confirmed",
        actor_type=ACTOR_PROVIDER,
        provider_reference=reference,
        rail_name="loop",
    )
    db.commit()

    (global_event,) = _events(db, None)
    linked = _events(db, transaction.id)[-1]
    body = json.loads(linked.payload)
    assert linked.event_type == payloads.GLOBAL_EVENT_LINKED
    assert body["global_leaf_hash"] == "0x" + bytes(global_event.leaf_hash).hex()
    assert body["global_audit_event_id"] == global_event.source_id
    _assert_chain(_events(db, transaction.id))


def test_a_rolled_back_posting_leaves_neither_rows_nor_events() -> None:
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="89"
    )
    escrow = EscrowService(db).escrow_for(transaction)
    db.commit()
    before = len(_events(db, transaction.id))

    EscrowLedger(db).post_movement(
        LedgerMovement(
            escrow_id=escrow.id,
            idempotency_key="rolled-back",
            amount=Decimal("10.00"),
            debit_account_code="ESCROW_HELD",
            credit_account_code="BUYER_CLEARING",
        ),
        commit=False,
    )
    assert len(_events(db, transaction.id)) == before + 2  # pending in the same DB transaction
    db.rollback()

    assert len(_events(db, transaction.id)) == before
    rows = db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == "rolled-back"))
    assert rows.first() is None


def test_status_ledger_and_audit_rows_are_only_written_at_their_choke_points() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in APP.rglob("*.py")}

    def writers(pattern: str) -> set[str]:
        constructor = re.compile(r"(?<!class )" + pattern)
        return {str(p.relative_to(APP)) for p, src in sources.items() if constructor.search(src)}

    assert writers(r"\.status = TransactionStatus\.") == set()
    assert writers(r"transaction\.status = ") == {"services/transitions.py"}
    assert writers(r"\bLedgerEntry\(") == {"services/ledger.py"}
    assert writers(r"\bMoneyAuditEvent\(") == {"services/audit.py"}
    assert writers(r"\bAnchorEvent\(") == {"services/anchoring/recorder.py"}


def test_production_requires_an_admin_ref_secret() -> None:
    base = {
        "environment": "production",
        "session_cookie_secure": True,
        "csrf_cookie_secure": True,
        "frontend_url": "https://frontend.example",
        "cors_origins": "https://frontend.example",
        "secret_key": "s",
        "database_url": "sqlite://",
        "alembic_database_url": "sqlite://",
        "phone_code_dev_echo": False,
        "cors_allow_credentials": True,
    }
    with pytest.raises(ValueError, match="ANCHOR_ADMIN_REF_SECRET"):
        Settings(**base, anchor_admin_ref_secret="")
    with pytest.raises(ValueError, match="ANCHOR_ADMIN_REF_SECRET"):
        Settings(**base, anchor_admin_ref_secret="too-short")
    assert Settings(**base, anchor_admin_ref_secret="k" * 32).environment == "production"


def test_admin_refs_survive_a_key_rotation() -> None:
    admin_id = uuid.uuid4()
    old, new = "o" * 32, "n" * 32
    ref = admin_ref(admin_id, keys=[old])
    assert admin_ref(admin_id, keys=[new, old]) != ref  # new events use the new key
    assert resolve_admin_ref(ref, [admin_id], keys=[new, old]) == admin_id
    assert resolve_admin_ref(ref, [admin_id], keys=[new]) is None


def test_backfill_anchors_existing_history_once_and_marks_it(monkeypatch) -> None:
    lenient = get_settings().model_copy(update={"anchor_strict_events": False})
    monkeypatch.setattr("app.core.settings.get_settings", lambda: lenient)
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="90"
    )
    # Simulate pre-M21 history: rows written with no anchor events at all.
    db.execute(text("DROP TRIGGER anchor_events_guard_delete"))
    db.execute(text("DELETE FROM anchor_events"))
    db.commit()
    db.info.pop("anchor_events_table_ready", None)
    record_money_audit_event(
        db, action="old_event", actor_type="system", transaction_id=transaction.id
    )
    db.commit()  # the fixture session does not autoflush
    db.execute(text("DELETE FROM anchor_events"))
    db.commit()

    result = backfill_anchor_events(db)

    assert (result.transactions_created, result.money_audit_events, result.snapshots) == (1, 1, 1)
    events = _events(db, transaction.id)
    _assert_chain(events)
    assert [e.event_type for e in events] == [
        payloads.TRANSACTION_CREATED,
        payloads.MONEY_AUDIT,
        payloads.BACKFILL_SNAPSHOT,
    ]
    assert all(json.loads(e.payload)["backfilled"] is True for e in events)
    snapshot = json.loads(events[-1].payload)
    assert snapshot["status"] == transaction.status.value
    assert [e.event_type for e in _events(db, None)] == [payloads.BACKFILL_COMPLETED]

    with pytest.raises(BackfillRefusedError):
        backfill_anchor_events(db)
    again = backfill_anchor_events(db, resume=True)
    assert (again.transactions_created, again.money_audit_events) == (0, 0)


def test_transition_helper_records_the_actor_and_reason() -> None:
    db = _db_with_audit()
    _buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="91"
    )
    transition_transaction(
        db, transaction, TransactionStatus.LOCKED, actor_type="user", actor_id=seller.id, reason="r"
    )
    db.commit()
    body = json.loads(_events(db, transaction.id)[-1].payload)
    assert (body["from"], body["to"], body["actor_id"], body["reason"]) == (
        "awaiting_payment",
        "locked",
        str(seller.id),
        "r",
    )
