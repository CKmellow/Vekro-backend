import uuid
from decimal import Decimal
from random import Random

import pytest
from app.db.base import Base
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry, LedgerEntrySide
from app.models.transaction import Transaction
from app.services.ledger import (
    ESCROW_HELD_ACCOUNT,
    EscrowLedger,
    EscrowLedgerIdempotencyConflictError,
    LedgerMovement,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Transaction.__table__,
            Escrow.__table__,
            LedgerEntry.__table__,
        ],
    )
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_post_movement_creates_balanced_debit_credit_pair(db_session: Session) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()

    result = ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="funding-1",
            amount=Decimal("1250.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
            description="Initial escrow funding",
        )
    )

    entries = list(
        db_session.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == "funding-1"))
        .scalars()
        .all()
    )

    assert result.created_entries == 2
    assert result.was_idempotent_replay is False
    assert len(entries) == 2

    debit_entry = next(entry for entry in entries if entry.side == LedgerEntrySide.DEBIT)
    credit_entry = next(entry for entry in entries if entry.side == LedgerEntrySide.CREDIT)

    assert debit_entry.amount == Decimal("1250.00")
    assert credit_entry.amount == Decimal("1250.00")
    assert debit_entry.account_code == ESCROW_HELD_ACCOUNT
    assert credit_entry.account_code == "BUYER_CLEARING"


def test_post_movement_is_safe_no_op_when_idempotency_key_exists(db_session: Session) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()

    first_result = ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="payout-1",
            amount=Decimal("300.00"),
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )
    )

    second_result = ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="payout-1",
            amount=Decimal("300.00"),
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )
    )

    entries = list(
        db_session.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key == "payout-1"))
        .scalars()
        .all()
    )

    assert first_result.created_entries == 2
    assert second_result.created_entries == 0
    assert second_result.was_idempotent_replay is True
    assert second_result.entry_group_id == first_result.entry_group_id
    assert len(entries) == 2


def test_post_movement_rejects_cross_escrow_idempotency_key_reuse(db_session: Session) -> None:
    ledger = EscrowLedger(db_session)

    ledger.post_movement(
        LedgerMovement(
            escrow_id=uuid.uuid4(),
            idempotency_key="shared-key",
            amount=Decimal("75.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
        )
    )

    with pytest.raises(EscrowLedgerIdempotencyConflictError):
        ledger.post_movement(
            LedgerMovement(
                escrow_id=uuid.uuid4(),
                idempotency_key="shared-key",
                amount=Decimal("75.00"),
                debit_account_code=ESCROW_HELD_ACCOUNT,
                credit_account_code="BUYER_CLEARING",
            )
        )


def test_get_escrow_held_balance_returns_debits_minus_credits(db_session: Session) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()

    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="funding-2",
            amount=Decimal("500.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
        )
    )
    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="release-2",
            amount=Decimal("180.00"),
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )
    )

    balance = ledger.get_escrow_held_balance(escrow_id)

    assert balance == Decimal("320.00")


def test_randomized_movement_sequences_never_unbalance_journal_totals(
    db_session: Session,
) -> None:
    ledger = EscrowLedger(db_session)
    rng = Random(67)
    escrow_ids = [uuid.uuid4() for _ in range(4)]
    operations = [
        (ESCROW_HELD_ACCOUNT, "BUYER_CLEARING"),
        ("SELLER_PAYABLE", ESCROW_HELD_ACCOUNT),
        ("BUYER_REFUNDABLE", ESCROW_HELD_ACCOUNT),
        (ESCROW_HELD_ACCOUNT, "FEE_REVENUE"),
    ]

    for index in range(75):
        debit_account_code, credit_account_code = operations[rng.randrange(len(operations))]
        amount = (Decimal(rng.randint(1, 50_000)) / Decimal("100")).quantize(Decimal("0.01"))

        ledger.post_movement(
            LedgerMovement(
                escrow_id=escrow_ids[rng.randrange(len(escrow_ids))],
                idempotency_key=f"rnd-{index}",
                amount=amount,
                debit_account_code=debit_account_code,
                credit_account_code=credit_account_code,
            )
        )

    all_entries = list(db_session.execute(select(LedgerEntry)).scalars().all())
    assert len(all_entries) == 150

    debit_total = sum(entry.amount for entry in all_entries if entry.side == LedgerEntrySide.DEBIT)
    credit_total = sum(
        entry.amount for entry in all_entries if entry.side == LedgerEntrySide.CREDIT
    )
    assert debit_total == credit_total


def test_escrow_held_lifecycle_invariant_funded_to_payout_completion(
    db_session: Session,
) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()

    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="lifecycle-fund",
            amount=Decimal("1000.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
        )
    )
    assert ledger.get_escrow_held_balance(escrow_id) == Decimal("1000.00")

    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="lifecycle-release-partial",
            amount=Decimal("250.00"),
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )
    )
    assert ledger.get_escrow_held_balance(escrow_id) == Decimal("750.00")

    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="lifecycle-release-final",
            amount=Decimal("750.00"),
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )
    )

    held_entries = list(
        db_session.execute(
            select(LedgerEntry).where(
                LedgerEntry.escrow_id == escrow_id,
                LedgerEntry.account_code == ESCROW_HELD_ACCOUNT,
            )
        )
        .scalars()
        .all()
    )
    held_debits = sum(entry.amount for entry in held_entries if entry.side == LedgerEntrySide.DEBIT)
    held_credits = sum(
        entry.amount for entry in held_entries if entry.side == LedgerEntrySide.CREDIT
    )

    assert ledger.get_escrow_held_balance(escrow_id) == Decimal("0.00")
    assert held_debits == held_credits


def test_append_only_invariant_keeps_existing_rows_while_adding_new_rows(
    db_session: Session,
) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()
    prior_ids: set[uuid.UUID] = set()

    for index in range(8):
        ledger.post_movement(
            LedgerMovement(
                escrow_id=escrow_id,
                idempotency_key=f"append-only-{index}",
                amount=Decimal("10.00"),
                debit_account_code=ESCROW_HELD_ACCOUNT,
                credit_account_code="BUYER_CLEARING",
            )
        )

        current_ids = set(db_session.execute(select(LedgerEntry.id)).scalars().all())
        assert prior_ids.issubset(current_ids)
        assert len(current_ids) == len(prior_ids) + 2
        prior_ids = current_ids


def test_duplicate_post_attempts_preserve_totals_and_row_counts(db_session: Session) -> None:
    ledger = EscrowLedger(db_session)
    escrow_id = uuid.uuid4()

    ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="dup-check",
            amount=Decimal("120.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
        )
    )

    before_entries = list(db_session.execute(select(LedgerEntry)).scalars().all())
    before_ids = {entry.id for entry in before_entries}
    before_debits = sum(
        entry.amount for entry in before_entries if entry.side == LedgerEntrySide.DEBIT
    )
    before_credits = sum(
        entry.amount for entry in before_entries if entry.side == LedgerEntrySide.CREDIT
    )
    before_balance = ledger.get_escrow_held_balance(escrow_id)

    replay_result = ledger.post_movement(
        LedgerMovement(
            escrow_id=escrow_id,
            idempotency_key="dup-check",
            amount=Decimal("120.00"),
            debit_account_code=ESCROW_HELD_ACCOUNT,
            credit_account_code="BUYER_CLEARING",
        )
    )

    after_entries = list(db_session.execute(select(LedgerEntry)).scalars().all())
    after_ids = {entry.id for entry in after_entries}
    after_debits = sum(
        entry.amount for entry in after_entries if entry.side == LedgerEntrySide.DEBIT
    )
    after_credits = sum(
        entry.amount for entry in after_entries if entry.side == LedgerEntrySide.CREDIT
    )
    after_balance = ledger.get_escrow_held_balance(escrow_id)

    assert replay_result.was_idempotent_replay is True
    assert replay_result.created_entries == 0
    assert len(after_entries) == len(before_entries)
    assert after_ids == before_ids
    assert after_debits == before_debits
    assert after_credits == before_credits
    assert after_balance == before_balance
