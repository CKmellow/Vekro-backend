"""Issue #221: the return-case timeouts move the dispute with the transaction, and the repair
command finds and fixes cases stranded before the fix."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.cli import main
from app.db.base import Base
from app.models.dispute import AdminDecision, Dispute, DisputeStatus, DisputeType
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import TransactionStatus
from app.services import transaction as transaction_service
from app.services.dispute import force_resolve_dispute_case, list_escalated_disputes
from app.services.return_case_repair import repair_stranded_return_cases
from app.services.transaction import (
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_buyer_sent_back,
    mark_transaction_arrived,
    report_functional_issue,
    run_timeout_jobs,
)
from app.services.transitions import transition_transaction
from sqlalchemy import Table, select, text
from sqlalchemy.orm import Session

from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _build_db_session,
    _seed_transaction_context,
)

LATER = timedelta(days=10)


def _db_with_audit() -> Session:
    db = _build_db_session()
    column = cast(Any, MoneyAuditEvent.__table__.c.details)
    original = column.server_default
    column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(db.get_bind(), tables=[cast(Table, MoneyAuditEvent.__table__)])
    finally:
        column.server_default = original
    return db


def _open_return_case(db: Session, monkeypatch, suffix: str):
    buyer, seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=True, suffix=suffix
    )
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
        description="Does not power on.",
    )
    dispute = db.execute(select(Dispute)).scalars().one()
    assert dispute.status == DisputeStatus.OPEN
    return buyer, seller, transaction, dispute


def test_seller_received_timeout_escalates_the_dispute_so_an_admin_can_resolve_it(
    monkeypatch,
) -> None:
    db = _db_with_audit()
    buyer, _seller, transaction, dispute = _open_return_case(db, monkeypatch, "41")
    mark_buyer_sent_back(db, transaction_id=transaction.id, buyer=buyer)

    now = datetime.now(UTC) + LATER
    result = run_timeout_jobs(db, now=now)

    assert result.dispute_seller_received_timeout_count == 1
    assert transaction.status == TransactionStatus.ESCALATED_ADMIN_REVIEW
    assert dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert dispute.escalated_at is not None
    assert [item.dispute_id for item in list_escalated_disputes(db)] == [dispute.id]

    resolved = force_resolve_dispute_case(
        db, dispute.id, decision=AdminDecision.REFUND, reason="Seller never confirmed receipt."
    )
    assert resolved.transaction_status == TransactionStatus.RESOLVED_REFUND
    purposes = [a.purpose for a in db.execute(select(PayoutAttempt)).scalars()]
    assert purposes == [f"admin-refund:{dispute.id}"]


def test_buyer_sent_back_timeout_closes_the_dispute_with_the_release(monkeypatch) -> None:
    db = _db_with_audit()
    _buyer, _seller, transaction, dispute = _open_return_case(db, monkeypatch, "42")

    now = datetime.now(UTC) + LATER
    result = run_timeout_jobs(db, now=now)

    assert result.dispute_buyer_sent_back_timeout_count == 1
    assert transaction.status == TransactionStatus.RELEASED
    assert dispute.status == DisputeStatus.CANCELED
    assert dispute.resolved_at is not None
    purposes = [a.purpose for a in db.execute(select(PayoutAttempt)).scalars()]
    assert purposes == [f"dispute-timeout-release:{transaction.id}"]
    assert list_escalated_disputes(db) == []


def _stranded(db: Session, suffix: str, status: TransactionStatus) -> Dispute:
    buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=True, suffix=suffix
    )
    # Recorded like any other transition: the strict anchor guard is on for this schema.
    transition_transaction(db, transaction, status, actor_type="system", reason="test_setup")
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.OPEN,
        reason="not_working",
        description="Broken.",
        evidence={},
    )
    db.add(dispute)
    db.commit()
    return dispute


def test_repair_lists_by_default_and_fixes_only_with_apply() -> None:
    db = _db_with_audit()
    escalate = _stranded(db, "51", TransactionStatus.ESCALATED_ADMIN_REVIEW)
    close = _stranded(db, "52", TransactionStatus.RELEASED)
    review = _stranded(db, "53", TransactionStatus.REFUNDED_BUYER)
    _stranded(db, "54", TransactionStatus.RETURN_IN_TRANSIT)  # still in progress: not stuck

    listed = repair_stranded_return_cases(db, apply=False)
    assert {(c.dispute_id, c.action) for c in listed} == {
        (escalate.id, "escalate"),
        (close.id, "close"),
        (review.id, "review"),
    }
    assert {escalate.status, close.status, review.status} == {DisputeStatus.OPEN}
    assert db.execute(select(MoneyAuditEvent)).scalars().all() == []

    repair_stranded_return_cases(db, apply=True)
    assert escalate.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert escalate.escalated_at is not None
    assert close.status == DisputeStatus.CANCELED
    assert close.resolved_at is not None
    assert review.status == DisputeStatus.OPEN
    audits = db.execute(select(MoneyAuditEvent)).scalars().all()
    assert sorted(a.details["to"] for a in audits) == ["canceled", "escalated_admin_review"]
    assert {a.action for a in audits} == {"return_case_repaired"}

    assert [c.action for c in repair_stranded_return_cases(db, apply=False)] == ["review"]


def test_repair_command_is_a_dry_run_without_apply(monkeypatch, capsys) -> None:
    db = _db_with_audit()
    stuck = _stranded(db, "61", TransactionStatus.ESCALATED_ADMIN_REVIEW)
    monkeypatch.setattr("app.cli._session", lambda: db)

    assert main(["repair-return-cases"]) == 0
    out = capsys.readouterr().out
    assert "-> escalate" in out and "dry run, nothing changed" in out
    assert stuck.status == DisputeStatus.OPEN
