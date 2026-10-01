import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.db.base import Base
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus
from app.models.user import User, UserRole
from app.services.escrow_service import (
    EscrowService,
    run_payout_executor_once,
)
from app.services.ledger import ESCROW_HELD_ACCOUNT
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _build_db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    json_default_columns = [
        Listing.__table__.c.dispute_policy,
        Notification.__table__.c.payload,
    ]
    original_defaults = {column: column.server_default for column in json_default_columns}
    for column in json_default_columns:
        column.server_default = text("'{}'")

    try:
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Listing.__table__,
                Transaction.__table__,
                Escrow.__table__,
                LedgerEntry.__table__,
                Notification.__table__,
            ],
        )
    finally:
        for column, default in original_defaults.items():
            column.server_default = default

    with engine.begin() as connection:
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

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return session_factory()


def _build_user(*, role: UserRole, phone: str) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} User",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=phone,
        mpesa_account_name=f"{role.value}-account",
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _seed_transaction(db: Session, *, status: TransactionStatus) -> Transaction:
    buyer = _build_user(role=UserRole.BUYER, phone="+254700000001")
    seller = _build_user(role=UserRole.SELLER, phone="+254700000002")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Escrow Service Listing",
        price=Decimal("2000.00"),
        is_serialized=False,
        unique_id=None,
        dispute_policy={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    transaction = Transaction(
        id=uuid.uuid4(),
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=Decimal("2000.00"),
        status=status,
        payout_status=TransactionPayoutStatus.NOT_REQUIRED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    db.add_all([buyer, seller, listing, transaction])
    db.commit()
    db.refresh(transaction)
    return transaction


def test_queue_release_sets_pending_and_creates_outbox_row() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)

        attempt = service.queue_release_full(
            transaction,
            purpose=f"tx-release:{transaction.id}",
        )
        db.commit()
        db.refresh(transaction)

        assert attempt is not None
        assert transaction.payout_status == TransactionPayoutStatus.PENDING

        attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        escrows = list(db.execute(select(Escrow)).scalars().all())

        assert len(attempts) == 1
        assert len(escrows) == 1
        assert attempts[0].purpose == f"tx-release:{transaction.id}"
        assert attempts[0].provider_reference is None
        assert attempts[0].request_snapshot["payout_kind"] == "release"
    finally:
        db.close()


def test_queue_release_is_idempotent_for_same_purpose() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)

        first = service.queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
        second = service.queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
        db.commit()

        attempts = list(db.execute(select(PayoutAttempt)).scalars().all())

        assert first is not None
        assert second is not None
        assert first.id == second.id
        assert len(attempts) == 1
    finally:
        db.close()


def test_executor_processes_pending_release_and_marks_succeeded() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)
        service.queue_release_full(transaction, purpose=f"tx-release:{transaction.id}")
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        run_result = run_payout_executor_once(db)
        db.refresh(transaction)

        attempt = db.execute(select(PayoutAttempt)).scalar_one()
        escrow = db.execute(select(Escrow)).scalar_one()
        movement_entries = list(
            db.execute(
                select(LedgerEntry).where(LedgerEntry.idempotency_key == f"outbox:{attempt.id}")
            )
            .scalars()
            .all()
        )

        assert run_result.claimed == 1
        assert run_result.succeeded == 1
        assert attempt.provider_reference is not None
        assert transaction.payout_status == TransactionPayoutStatus.SUCCEEDED
        assert escrow.released_amount == transaction.amount
        assert len(movement_entries) == 2
        assert all(
            entry.account_code in {"SELLER_PAYABLE", ESCROW_HELD_ACCOUNT}
            for entry in movement_entries
        )
    finally:
        db.close()
