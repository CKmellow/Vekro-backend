import uuid
from decimal import Decimal

from app.db.base import Base
from app.models.collection_attempt import AttemptOutcome, CollectionAttempt
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _build_db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Transaction.__table__,
            Escrow.__table__,
            LedgerEntry.__table__,
            # CollectionAttempt.__table__,
            # PayoutAttempt.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(text("""
                CREATE TABLE collection_attempts (
                    id TEXT PRIMARY KEY,
                    escrow_id TEXT NOT NULL,
                    rail_name VARCHAR(40) NOT NULL,
                    provider_name VARCHAR(40) NOT NULL,
                    idempotency_key VARCHAR(120) NOT NULL,
                    provider_reference VARCHAR(120),
                    amount NUMERIC(12, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'KES',
                    outcome VARCHAR(15) NOT NULL DEFAULT 'unknown',
                    request_snapshot JSON NOT NULL DEFAULT '{}',
                    response_snapshot JSON NOT NULL DEFAULT '{}',
                    failure_code VARCHAR(64),
                    failure_reason VARCHAR(255),
                    attempted_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT ck_collection_attempts_amount_positive CHECK (amount > 0)
                )
                """))
        connection.execute(text("""
                CREATE TABLE payout_attempts (
                    id TEXT PRIMARY KEY,
                    escrow_id TEXT NOT NULL,
                    purpose VARCHAR(64) NOT NULL,
                    rail_name VARCHAR(40) NOT NULL,
                    provider_name VARCHAR(40) NOT NULL,
                    idempotency_key VARCHAR(120) NOT NULL,
                    provider_reference VARCHAR(120),
                    amount NUMERIC(12, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL DEFAULT 'KES',
                    outcome VARCHAR(15) NOT NULL DEFAULT 'unknown',
                    request_snapshot JSON NOT NULL DEFAULT '{}',
                    response_snapshot JSON NOT NULL DEFAULT '{}',
                    failure_code VARCHAR(64),
                    failure_reason VARCHAR(255),
                    attempted_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT ck_payout_attempts_amount_positive CHECK (amount > 0)
                )
                """))
        connection.execute(text("""
                CREATE UNIQUE INDEX ux_payout_attempts_one_non_failed_per_escrow_purpose
                ON payout_attempts (escrow_id, purpose)
                WHERE outcome <> 'failed_definite'
                """))

    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return SessionLocal()


def test_provider_capabilities_report_non_structural_holds_false() -> None:
    db = _build_db_session()
    try:
        provider = SimulatedCustodyProvider(db)
        capabilities = provider.capabilities()

        assert capabilities.holds_funds_structurally is False
    finally:
        db.close()


def test_provider_methods_and_auditable_attempt_persistence() -> None:
    db = _build_db_session()
    try:
        rail = SimulatedRail()
        provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)

        open_request = OpenEscrowRequest(
            transaction_id=str(uuid.uuid4()),
            buyer_id=str(uuid.uuid4()),
            seller_id=str(uuid.uuid4()),
            amount=Decimal("1000.00"),
        )
        escrow_record = provider.open_escrow(open_request)

        funding_result = provider.request_funding(
            FundingRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("1000.00"),
                phone_number="+254712345678",
                account_reference="order-69",
            )
        )
        release_result = provider.release(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("300.00"),
                destination_phone="+254712345678",
                purpose="release-69",
            )
        )
        refund_result = provider.refund(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("200.00"),
                destination_phone="+254712345678",
                purpose="refund-69",
            )
        )

        status = provider.get_status(escrow_record.escrow_reference)

        collection_attempts = list(db.execute(select(CollectionAttempt)).scalars().all())
        payout_attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        ledger_entries = list(db.execute(select(LedgerEntry)).scalars().all())

        assert funding_result.outcome == CollectionOutcome.SUCCEEDED
        assert release_result.outcome == PayoutOutcome.SUCCEEDED
        assert refund_result.outcome == PayoutOutcome.SUCCEEDED

        assert len(collection_attempts) == 1
        assert len(payout_attempts) == 2
        assert len(ledger_entries) == 6

        assert collection_attempts[0].outcome == AttemptOutcome.SUCCEEDED
        assert (
            collection_attempts[0].request_snapshot["escrow_reference"]
            == escrow_record.escrow_reference
        )
        assert (
            collection_attempts[0].response_snapshot["provider_reference"]
            == funding_result.provider_reference
        )

        assert all(attempt.outcome == AttemptOutcome.SUCCEEDED for attempt in payout_attempts)
        assert all("provider_reference" in attempt.response_snapshot for attempt in payout_attempts)

        assert status.funded_amount == Decimal("1000.00")
        assert status.released_amount == Decimal("300.00")
        assert status.refunded_amount == Decimal("200.00")
    finally:
        db.close()


def test_provider_distinguishes_unknown_and_failed_outcomes() -> None:
    db = _build_db_session()
    try:
        rail = SimulatedRail()
        provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)

        escrow_record = provider.open_escrow(
            OpenEscrowRequest(
                transaction_id=str(uuid.uuid4()),
                buyer_id=str(uuid.uuid4()),
                seller_id=str(uuid.uuid4()),
                amount=Decimal("450.00"),
            )
        )

        timeout_funding = provider.request_funding(
            FundingRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("100.00"),
                phone_number="+254712345678",
                account_reference="sim:timeout",
            )
        )
        failed_refund = provider.refund(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("100.00"),
                destination_phone="+254712345678",
                purpose="sim:failed_definite",
            )
        )

        collection_attempt = db.execute(select(CollectionAttempt)).scalar_one()
        payout_attempt = db.execute(select(PayoutAttempt)).scalar_one()
        ledger_entries = list(db.execute(select(LedgerEntry)).scalars().all())
        status = provider.get_status(escrow_record.escrow_reference)

        assert timeout_funding.outcome == CollectionOutcome.UNKNOWN
        assert failed_refund.outcome == PayoutOutcome.FAILED_DEFINITE

        assert collection_attempt.outcome == AttemptOutcome.UNKNOWN
        assert payout_attempt.outcome == AttemptOutcome.FAILED_DEFINITE

        assert len(ledger_entries) == 0
        assert status.funded_amount == Decimal("0.00")
        assert status.released_amount == Decimal("0.00")
        assert status.refunded_amount == Decimal("0.00")
    finally:
        db.close()


def test_duplicate_success_payout_reuses_attempt_and_preserves_single_ledger_post() -> None:
    db = _build_db_session()
    try:
        rail = SimulatedRail()
        provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)

        escrow_record = provider.open_escrow(
            OpenEscrowRequest(
                transaction_id=str(uuid.uuid4()),
                buyer_id=str(uuid.uuid4()),
                seller_id=str(uuid.uuid4()),
                amount=Decimal("900.00"),
            )
        )
        provider.request_funding(
            FundingRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("900.00"),
                phone_number="+254712345678",
                account_reference="order-dup",
            )
        )

        first = provider.release(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("150.00"),
                destination_phone="+254712345678",
                purpose="sim:duplicate",
            )
        )
        second = provider.release(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=Decimal("150.00"),
                destination_phone="+254712345678",
                purpose="sim:duplicate",
            )
        )

        payout_attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        release_ledger_entries = list(
            db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key.like("sim-release:%")))
            .scalars()
            .all()
        )
        status = provider.get_status(escrow_record.escrow_reference)

        assert first.outcome == PayoutOutcome.SUCCEEDED
        assert second.outcome == PayoutOutcome.SUCCEEDED
        assert len(payout_attempts) == 1
        assert len(release_ledger_entries) == 2
        assert status.released_amount == Decimal("150.00")
    finally:
        db.close()
