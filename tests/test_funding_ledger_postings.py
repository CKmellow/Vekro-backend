"""Issue #222: every funded escrow has its funding on the ledger (ESCROW_HELD == funded_amount), on
the simulated, eConfirm and payment-callback paths; the callback records funding only on the
simulated rail outside production; and the repair command posts what is missing."""

from datetime import UTC, datetime
from decimal import Decimal

from app.cli import main
from app.core.settings import get_settings
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionStatus
from app.services import transaction as transaction_service
from app.services.custody.dto import CollectionResult
from app.services.custody.econfirm_orchestration import run_econfirm_funding_reconciliation
from app.services.custody.enums import CollectionOutcome
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from app.services.escrow_service import EscrowService, run_payout_executor_once
from app.services.funding_ledger_repair import (
    escrow_held_funded,
    find_funding_ledger_gaps,
    repair_funding_ledger,
)
from app.services.ledger import EscrowLedger, LedgerMovement
from app.services.transaction import confirm_payment_callback
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_return_case_timeouts import _db_with_audit
from tests.test_simulated_custody_full_flow import (
    _build_callback_payload,
    _open_and_fund_escrow,
    _seed_transaction_context,
)


def _assert_funding_on_ledger(db: Session) -> None:
    """The invariant: for every funded escrow, ESCROW_HELD money in equals funded_amount, and the
    ESCROW_HELD balance equals what is still held."""
    funded = db.execute(select(Escrow).where(Escrow.funded_amount > 0)).scalars().all()
    assert funded, "expected at least one funded escrow"
    for escrow in funded:
        assert escrow_held_funded(db, escrow.id) == escrow.funded_amount
        held = escrow.funded_amount - escrow.released_amount - escrow.refunded_amount
        assert EscrowLedger(db).get_escrow_held_balance(escrow.id) == held


def _ledger_rows(db: Session) -> list[LedgerEntry]:
    return list(db.execute(select(LedgerEntry)).scalars())


def _audit_actions(db: Session) -> list[str]:
    return [event.action for event in db.execute(select(MoneyAuditEvent)).scalars()]


def _production(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"environment": "production"})
    monkeypatch.setattr(transaction_service, "get_settings", lambda: settings)


def test_simulated_provider_funding_is_not_posted_again_by_the_callback() -> None:
    db = _db_with_audit()
    buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="71"
    )
    provider = SimulatedCustodyProvider(
        db, collection_rail=SimulatedRail(), payout_rail=SimulatedRail()
    )
    _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)

    confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))

    assert transaction.status == TransactionStatus.LOCKED
    assert len(_ledger_rows(db)) == 2  # the provider's funding only
    assert "callback_funding_recorded" not in _audit_actions(db)
    _assert_funding_on_ledger(db)


def test_callback_records_simulated_funding_once_outside_production() -> None:
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="72"
    )

    first = confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    second = confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
    transaction_service._record_simulated_callback_funding(db, transaction, settings=get_settings())
    db.commit()

    assert first.transitioned and second.duplicate
    escrow = db.execute(select(Escrow)).scalar_one()
    assert escrow.funded_amount == transaction.amount
    assert [row.idempotency_key for row in _ledger_rows(db)] == [
        f"callback-funding:{transaction.id}"
    ] * 2
    assert _audit_actions(db).count("callback_funding_recorded") == 1
    _assert_funding_on_ledger(db)

    # The funded escrow now settles a release through the executor.
    EscrowService(db).queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
    db.commit()
    assert run_payout_executor_once(db).succeeded == 1
    _assert_funding_on_ledger(db)
    assert EscrowLedger(db).get_escrow_held_balance(escrow.id) == Decimal("0.00")


def test_callback_never_records_funding_in_production_and_the_executor_refuses(
    monkeypatch,
) -> None:
    _production(monkeypatch)
    db = _db_with_audit()
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix="73"
    )

    result = confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))

    assert result.transitioned and transaction.status == TransactionStatus.LOCKED
    assert _ledger_rows(db) == []
    assert db.execute(select(Escrow)).scalars().all() == []
    assert "callback_funding_recorded" not in _audit_actions(db)

    EscrowService(db).queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
    db.commit()
    run = run_payout_executor_once(db)
    attempt = db.execute(select(PayoutAttempt)).scalar_one()
    assert run.succeeded == 0 and run.unknown == 1
    assert attempt.failure_code == "insufficient_funded_balance"
    assert _ledger_rows(db) == []


def _econfirm_transaction(db: Session, suffix: str) -> tuple[Transaction, Escrow]:
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    transaction.custody_provider = "econfirm"
    escrow = Escrow(
        transaction_id=transaction.id,
        buyer_id=transaction.buyer_id,
        seller_id=transaction.seller_id,
        amount_total=transaction.amount,
        currency="KES",
        custody_provider="econfirm",
        econfirm_transaction_id=f"txn_{suffix}",
        created_at=datetime.now(UTC),
    )
    db.add(escrow)
    db.commit()
    return transaction, escrow


class _FundedRail:
    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="Escrow Funded",
        )


def test_econfirm_poller_posts_funding_with_the_lock() -> None:
    db = _db_with_audit()
    transaction, escrow = _econfirm_transaction(db, "74")

    first = run_econfirm_funding_reconciliation(db, collection_rail=_FundedRail())
    second = run_econfirm_funding_reconciliation(db, collection_rail=_FundedRail())

    assert first.funded == 1 and second.polled == 0
    assert transaction.status == TransactionStatus.LOCKED
    assert escrow.funded_amount == transaction.amount
    assert {row.idempotency_key for row in _ledger_rows(db)} == {f"econfirm-funding:{escrow.id}"}
    assert len(_ledger_rows(db)) == 2
    _assert_funding_on_ledger(db)


def test_callback_ignores_econfirm_custody() -> None:
    db = _db_with_audit()
    transaction, escrow = _econfirm_transaction(db, "75")

    result = confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))

    assert not result.transitioned
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert escrow.funded_amount == Decimal("0.00")
    assert _ledger_rows(db) == []


def test_repair_posts_missing_funding_only_with_apply() -> None:
    db = _db_with_audit()
    _txn, missing = _econfirm_transaction(db, "76")
    missing.funded_amount = missing.amount_total  # as the poller did before #222
    _txn2, over = _econfirm_transaction(db, "77")
    EscrowLedger(db).post_movement(
        LedgerMovement(
            escrow_id=over.id,
            idempotency_key="test-over",
            amount=over.amount_total,
            debit_account_code="ESCROW_HELD",
            credit_account_code="BUYER_CLEARING",
        )
    )
    over.funded_amount = (over.amount_total / 2).quantize(Decimal("0.01"))
    db.commit()

    listed = repair_funding_ledger(db, apply=False)
    assert {(gap.escrow_id, gap.action) for gap in listed} == {
        (missing.id, "post"),
        (over.id, "review"),
    }
    assert escrow_held_funded(db, missing.id) == Decimal("0.00")

    repair_funding_ledger(db, apply=True)
    assert escrow_held_funded(db, missing.id) == missing.funded_amount
    assert [gap.escrow_id for gap in find_funding_ledger_gaps(db)] == [over.id]
    assert _audit_actions(db) == ["funding_ledger_repaired"]

    repair_funding_ledger(db, apply=True)  # nothing new to post
    assert len([r for r in _ledger_rows(db) if r.escrow_id == missing.id]) == 2


def test_repair_command_is_a_dry_run_without_apply(monkeypatch, capsys) -> None:
    db = _db_with_audit()
    _txn, escrow = _econfirm_transaction(db, "78")
    escrow.funded_amount = escrow.amount_total
    db.commit()
    monkeypatch.setattr("app.cli._session", lambda: db)

    assert main(["repair-funding-ledger"]) == 0
    out = capsys.readouterr().out
    assert "-> post" in out and "dry run, nothing changed" in out
    assert escrow_held_funded(db, escrow.id) == Decimal("0.00")
