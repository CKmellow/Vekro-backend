import uuid
from decimal import Decimal

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
