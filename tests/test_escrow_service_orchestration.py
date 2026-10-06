import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from app.db.base import Base
from app.models.collection_attempt import AttemptOutcome
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.payout_attempt import PayoutAttempt
from app.models.rail_health import RailBreakerState, RailHealth
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus
from app.models.user import User, UserRole
from app.services.custody.dto import CustodyCapabilities, PayoutRequest, PayoutResult
from app.services.custody.enums import CustodyMode, PayoutOutcome
from app.services.custody.registry import ConfiguredCustodyProvider, CustodyRegistry
from app.services.custody.simulated_rail import SimulatedRail
from app.services.escrow_service import (
    EscrowService,
    SplitPayoutUnsupportedError,
    run_payout_executor_once,
    run_payout_reconciliation,
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
        connection.execute(text("""
            CREATE TABLE rail_health (
                id TEXT PRIMARY KEY,
                rail_name VARCHAR(40) NOT NULL,
                provider_name VARCHAR(40),
                breaker_state VARCHAR(20) NOT NULL DEFAULT 'closed',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error_code VARCHAR(64),
                last_error_message VARCHAR(255),
                opened_at DATETIME,
                last_success_at DATETIME,
                last_failure_at DATETIME,
                cooldown_until DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """))
        connection.execute(text("""
            CREATE UNIQUE INDEX ix_rail_health_rail_name
            ON rail_health (rail_name)
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


def test_executor_retries_unknown_with_same_provider_reference() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)
        service.queue_release_full(
            transaction,
            purpose=f"sim:out_of_order tx-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        first = run_payout_executor_once(db)
        attempt_after_first = db.execute(select(PayoutAttempt)).scalar_one()
        reference = attempt_after_first.provider_reference

        second = run_payout_executor_once(db)
        attempt_after_second = db.execute(select(PayoutAttempt)).scalar_one()

        third = run_payout_executor_once(db)
        attempt_after_third = db.execute(select(PayoutAttempt)).scalar_one()
        db.refresh(transaction)

        assert first.unknown == 1
        assert second.unknown == 1
        assert third.succeeded == 1
        assert reference is not None
        assert attempt_after_second.provider_reference == reference
        assert attempt_after_third.provider_reference == reference
        assert transaction.payout_status == TransactionPayoutStatus.SUCCEEDED
    finally:
        db.close()


def test_executor_marks_timeout_outcome_as_unknown_status() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)
        service.queue_release_full(
            transaction,
            purpose=f"sim:timeout tx-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        run_result = run_payout_executor_once(db)
        attempt = db.execute(select(PayoutAttempt)).scalar_one()
        db.refresh(transaction)

        assert run_result.claimed == 1
        assert run_result.unknown == 1
        assert attempt.outcome == AttemptOutcome.UNKNOWN
        assert attempt.provider_reference is not None
        assert transaction.payout_status == TransactionPayoutStatus.UNKNOWN
    finally:
        db.close()


def test_executor_keeps_malformed_unknown_without_automatic_failover() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)
        service.queue_release_full(
            transaction,
            purpose=f"sim:malformed tx-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        first = run_payout_executor_once(db)
        second = run_payout_executor_once(db)

        attempts = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        attempt = attempts[0]
        db.refresh(transaction)

        assert first.unknown == 1
        assert second.unknown == 1
        assert len(attempts) == 1
        assert attempt.outcome == AttemptOutcome.UNKNOWN
        assert attempt.failure_code == "malformed_response"
        assert attempt.response_snapshot.get("raw_status") == "malformed_response"
        assert "unknown_retry_count" not in attempt.response_snapshot
        assert transaction.payout_status == TransactionPayoutStatus.UNKNOWN
    finally:
        db.close()


def test_executor_marks_failed_definite_status_when_rail_declines() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        service = EscrowService(db)
        service.queue_release_full(
            transaction,
            purpose=f"sim:failed tx-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        run_result = run_payout_executor_once(db)
        attempt = db.execute(select(PayoutAttempt)).scalar_one()
        ledger_entries = list(
            db.execute(
                select(LedgerEntry).where(LedgerEntry.idempotency_key == f"outbox:{attempt.id}")
            )
            .scalars()
            .all()
        )
        db.refresh(transaction)

        assert run_result.claimed == 1
        assert run_result.failed_definite == 1
        assert attempt.outcome == AttemptOutcome.FAILED_DEFINITE
        assert transaction.payout_status == TransactionPayoutStatus.FAILED_DEFINITE
        assert ledger_entries == []
    finally:
        db.close()


def test_unknown_payout_retries_same_reference_before_failover() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)

        timeout_rail = SimulatedRail(payout_default_scenario="timeout")
        fallback_rail = SimulatedRail(payout_default_scenario="success")
        registry = CustodyRegistry(
            provider=ConfiguredCustodyProvider(custody_mode=CustodyMode.TIER_2),
            collection_rails={"simulated": fallback_rail},
            payout_rails={"loop": timeout_rail, "simulated": fallback_rail},
            custody_mode=CustodyMode.TIER_2,
            collection_priority=("simulated",),
            payout_priority=("loop", "simulated"),
            live_payouts_enabled=True,
        )

        service = EscrowService(db, registry=registry)
        service.queue_release_full(
            transaction,
            purpose=f"unknown-failover-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        first_run = run_payout_executor_once(db)
        attempts_after_first = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        first_attempt = attempts_after_first[0]

        assert first_run.claimed == 1
        assert first_attempt.outcome == AttemptOutcome.UNKNOWN
        assert first_attempt.provider_reference is not None
        assert first_attempt.response_snapshot.get("unknown_retry_count") == 1
        assert len(attempts_after_first) == 1

        second_run = run_payout_executor_once(db)
        attempts_after_second = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        first_attempt = attempts_after_second[0]
        fallback_attempt = attempts_after_second[1]

        assert second_run.failed_definite == 1
        assert first_attempt.outcome == AttemptOutcome.FAILED_DEFINITE
        assert first_attempt.failure_code == "unknown_retry_exhausted"
        assert first_attempt.response_snapshot.get("failover_queued") is True
        assert fallback_attempt.rail_name == "simulated"
        assert fallback_attempt.outcome == AttemptOutcome.UNKNOWN
        assert fallback_attempt.provider_reference is None

        third_run = run_payout_executor_once(db)
        attempts_after_third = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        db.refresh(transaction)

        assert third_run.succeeded == 1
        assert attempts_after_third[1].outcome == AttemptOutcome.SUCCEEDED
        assert transaction.payout_status == TransactionPayoutStatus.SUCCEEDED
    finally:
        db.close()


def test_auth_failure_trips_breaker_and_routes_to_next_rail() -> None:
    class _AuthFailingRail:
        def request_payout(self, request: PayoutRequest) -> PayoutResult:
            _ = request
            raise RuntimeError("invalid credentials for token refresh")

        def get_payout_status(self, provider_reference: str) -> PayoutResult:
            return PayoutResult(
                outcome=PayoutOutcome.UNKNOWN,
                provider_reference=provider_reference,
                raw_status="unknown",
                message="status unknown",
            )

    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)

        fallback_rail = SimulatedRail(payout_default_scenario="success")
        registry = CustodyRegistry(
            provider=ConfiguredCustodyProvider(custody_mode=CustodyMode.TIER_2),
            collection_rails={"simulated": fallback_rail},
            payout_rails={"loop": _AuthFailingRail(), "simulated": fallback_rail},
            custody_mode=CustodyMode.TIER_2,
            collection_priority=("simulated",),
            payout_priority=("loop", "simulated"),
            live_payouts_enabled=True,
        )

        service = EscrowService(db, registry=registry)
        service.queue_release_full(
            transaction,
            purpose=f"auth-failover-release:{transaction.id}",
        )
        db.commit()

        escrow = db.execute(select(Escrow)).scalar_one()
        escrow.funded_amount = transaction.amount
        db.commit()

        first_run = run_payout_executor_once(db)
        attempts_after_first = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        first_attempt = attempts_after_first[0]
        fallback_attempt = attempts_after_first[1]
        breaker = db.execute(select(RailHealth).where(RailHealth.rail_name == "loop")).scalar_one()

        assert first_run.failed_definite == 1
        assert first_attempt.outcome == AttemptOutcome.FAILED_DEFINITE
        assert first_attempt.failure_code == "auth_failure"
        assert first_attempt.response_snapshot.get("failover_queued") is True
        assert fallback_attempt.rail_name == "simulated"
        assert fallback_attempt.outcome == AttemptOutcome.UNKNOWN
        assert breaker.breaker_state == RailBreakerState.OPEN

        second_run = run_payout_executor_once(db)
        attempts_after_second = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.created_at.asc())).scalars().all()
        )
        db.refresh(transaction)

        assert second_run.succeeded == 1
        assert attempts_after_second[1].outcome == AttemptOutcome.SUCCEEDED
        assert {attempt.rail_name for attempt in attempts_after_second}.issubset(
            {"loop", "simulated"}
        )
        assert transaction.payout_status == TransactionPayoutStatus.SUCCEEDED
    finally:
        db.close()


def test_queue_split_unsupported_raises_without_side_effects() -> None:
    class _Provider:
        def capabilities(self) -> CustodyCapabilities:
            return CustodyCapabilities(
                holds_funds_structurally=False,
                supports_split_payout=False,
                supports_partial_release=False,
                supports_webhook_auth=False,
            )

    class _Registry:
        provider = _Provider()
        payout_priority = ("loop",)

        @staticmethod
        def select_payout_rail(*, exclude=None):
            if exclude and "loop" in exclude:
                raise RuntimeError("No available payout rails are routable.")
            return "loop", object()

    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.ESCALATED_ADMIN_REVIEW)
        service = EscrowService(db, registry=cast(CustodyRegistry, _Registry()))

        with pytest.raises(SplitPayoutUnsupportedError):
            service.queue_split_payout(
                transaction,
                release_purpose=f"admin-split-release:{uuid.uuid4()}",
                refund_purpose=f"admin-split-refund:{uuid.uuid4()}",
                split_ratio=Decimal("0.5000"),
            )

        attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        db.refresh(transaction)

        assert attempts == []
        assert transaction.payout_status == TransactionPayoutStatus.NOT_REQUIRED
    finally:
        db.close()


def test_queue_split_generates_two_intents_with_ratio_checks() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.ESCALATED_ADMIN_REVIEW)
        service = EscrowService(db)

        release_attempt, refund_attempt = service.queue_split_payout(
            transaction,
            release_purpose=f"admin-split-release:{transaction.id}",
            refund_purpose=f"admin-split-refund:{transaction.id}",
            split_ratio=Decimal("0.5000"),
        )
        db.commit()
        db.refresh(transaction)

        attempts = list(
            db.execute(select(PayoutAttempt).order_by(PayoutAttempt.purpose.asc())).scalars().all()
        )

        assert release_attempt is not None
        assert refund_attempt is not None
        assert len(attempts) == 2
        assert attempts[0].purpose.startswith("admin-split-refund:")
        assert attempts[1].purpose.startswith("admin-split-release:")
        assert attempts[0].amount == Decimal("1000.00")
        assert attempts[1].amount == Decimal("1000.00")
        assert transaction.payout_status == TransactionPayoutStatus.PENDING
    finally:
        db.close()


def test_queue_split_is_idempotent_for_retried_admin_calls() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.ESCALATED_ADMIN_REVIEW)
        service = EscrowService(db)

        service.queue_split_payout(
            transaction,
            release_purpose=f"admin-split-release:{transaction.id}",
            refund_purpose=f"admin-split-refund:{transaction.id}",
            split_ratio=Decimal("0.5000"),
        )
        service.queue_split_payout(
            transaction,
            release_purpose=f"admin-split-release:{transaction.id}",
            refund_purpose=f"admin-split-refund:{transaction.id}",
            split_ratio=Decimal("0.5000"),
        )
        db.commit()

        attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        assert len(attempts) == 2
    finally:
        db.close()


def test_reconciliation_flags_unresolved_unknown_for_admin() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        admin = _build_user(role=UserRole.ADMIN, phone="+254700000099")
        db.add(admin)
        db.commit()

        service = EscrowService(db)
        service.queue_release_full(
            transaction,
            purpose=f"sim:timeout tx-release:{transaction.id}",
        )
        db.commit()

        run_payout_executor_once(db)
        reconciliation = run_payout_reconciliation(
            db,
            unknown_age_minutes=1,
            now=datetime.now(UTC) + timedelta(minutes=5),
        )

        notifications = list(
            db.execute(
                select(Notification).where(
                    Notification.user_id == admin.id,
                    Notification.transaction_id == transaction.id,
                )
            )
            .scalars()
            .all()
        )

        assert reconciliation.unknown_alerts == 1
        assert len(notifications) == 1
        assert notifications[0].event_type == NotificationEventType.SYSTEM_TIMEOUT
        assert notifications[0].payload["alert_key"].startswith("payout-unknown:")
    finally:
        db.close()


def test_reconciliation_flags_terminal_status_mismatch_for_admin() -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=TransactionStatus.RELEASED)
        admin = _build_user(role=UserRole.ADMIN, phone="+254700000088")
        db.add(admin)
        db.commit()

        reconciliation = run_payout_reconciliation(db, unknown_age_minutes=1)

        notifications = list(
            db.execute(
                select(Notification).where(
                    Notification.user_id == admin.id,
                    Notification.transaction_id == transaction.id,
                )
            )
            .scalars()
            .all()
        )

        assert reconciliation.mismatch_alerts == 1
        assert len(notifications) == 1
        assert notifications[0].event_type == NotificationEventType.SYSTEM_TIMEOUT
        assert notifications[0].payload["alert_key"].startswith("payout-mismatch:")
    finally:
        db.close()
