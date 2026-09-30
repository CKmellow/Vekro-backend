import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.db.base import Base
from app.models.collection_attempt import AttemptOutcome, CollectionAttempt
from app.models.dispute import AdminDecision, Dispute, DisputeStatus
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import PaymentCallbackRequest
from app.services import transaction as transaction_service
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from app.services.dispute import force_resolve_dispute_case
from app.services.transaction import (
    apply_seller_resolution_action,
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_buyer_sent_back,
    mark_seller_received,
    mark_transaction_arrived,
    report_functional_issue,
    submit_buyer_reconfirmation,
    withhold_buyer_delivery_otp,
)
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
        Dispute.__table__.c.evidence,
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
                Dispute.__table__,
                Notification.__table__,
                Escrow.__table__,
                LedgerEntry.__table__,
            ],
        )
    finally:
        for column, default in original_defaults.items():
            column.server_default = default

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

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return session_factory()


def _build_user(*, role: UserRole, suffix: str) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} {suffix}",
        phone=f"+2547000{suffix}",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=f"+2547123{suffix}",
        mpesa_account_name=f"{role.value}-acct-{suffix}",
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_listing(*, seller_id: uuid.UUID, serialized: bool) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=seller_id,
        title="Simulated Flow Listing",
        price=Decimal("2400.00"),
        is_serialized=serialized,
        unique_id="SER-71" if serialized else None,
        dispute_policy={
            "resolution": "seller_review",
            "allowed_seller_actions": [
                "refund_issued",
                "repair_shipped",
                "replacement_shipped",
            ],
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_callback_payload(transaction_id: uuid.UUID) -> PaymentCallbackRequest:
    return PaymentCallbackRequest(
        transaction_id=transaction_id,
        result_code=0,
        result_desc="The service request is processed successfully.",
        checkout_request_id="ws_CO_71",
        merchant_request_id="29115-34620561-71",
        provider_reference="simulated-callback-71",
    )


def _open_and_fund_escrow(
    db: Session,
    provider: SimulatedCustodyProvider,
    *,
    transaction: Transaction,
    buyer: User,
):
    escrow_record = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id=str(transaction.id),
            buyer_id=str(transaction.buyer_id),
            seller_id=str(transaction.seller_id),
            amount=transaction.amount,
        )
    )
    funding_result = provider.request_funding(
        FundingRequest(
            escrow_reference=escrow_record.escrow_reference,
            amount=transaction.amount,
            phone_number=buyer.mpesa_phone or buyer.phone,
            account_reference=f"issue71-{transaction.id}",
        )
    )
    assert funding_result.outcome == CollectionOutcome.SUCCEEDED
    return escrow_record


def _seed_transaction_context(
    db: Session,
    *,
    serialized_listing: bool,
    suffix: str,
) -> tuple[User, User, Listing, Transaction]:
    buyer = _build_user(role=UserRole.BUYER, suffix=f"1{suffix}")
    seller = _build_user(role=UserRole.SELLER, suffix=f"2{suffix}")
    listing = _build_listing(seller_id=seller.id, serialized=serialized_listing)
    now = datetime.now(UTC)
    transaction = Transaction(
        id=uuid.uuid4(),
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=listing.price,
        status=TransactionStatus.AWAITING_PAYMENT,
        created_at=now,
        updated_at=now,
    )

    db.add(buyer)
    db.add(seller)
    db.add(listing)
    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    return buyer, seller, listing, transaction


def test_full_flow_happy_path_release_with_simulated_provider(monkeypatch) -> None:
    db = _build_db_session()
    try:
        buyer, seller, _listing, transaction = _seed_transaction_context(
            db,
            serialized_listing=False,
            suffix="01",
        )
        provider = SimulatedCustodyProvider(
            db,
            collection_rail=SimulatedRail(),
            payout_rail=SimulatedRail(),
        )
        escrow_record = _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)

        callback_result = confirm_payment_callback(
            db,
            payload=_build_callback_payload(transaction.id),
        )
        assert callback_result.transitioned is True

        dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
        monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "123456")
        mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
        confirm_buyer_delivery_otp(
            db,
            transaction_id=transaction.id,
            buyer=buyer,
            otp_code="123456",
        )

        release_result = provider.release(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=transaction.amount,
                destination_phone=seller.mpesa_phone or seller.phone,
                purpose=f"tx-release:{transaction.id}",
            )
        )

        collection_attempts = list(db.execute(select(CollectionAttempt)).scalars().all())
        payout_attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        ledger_entries = list(db.execute(select(LedgerEntry)).scalars().all())
        status = provider.get_status(escrow_record.escrow_reference)

        assert release_result.outcome == PayoutOutcome.SUCCEEDED
        assert transaction.status == TransactionStatus.RELEASED
        assert len(collection_attempts) == 1
        assert len(payout_attempts) == 1
        assert collection_attempts[0].outcome == AttemptOutcome.SUCCEEDED
        assert payout_attempts[0].outcome == AttemptOutcome.SUCCEEDED
        assert payout_attempts[0].purpose == f"tx-release:{transaction.id}"
        assert len(ledger_entries) == 4
        assert status.funded_amount == transaction.amount
        assert status.released_amount == transaction.amount
        assert status.refunded_amount == Decimal("0.00")
    finally:
        db.close()


def test_full_flow_refund_path_with_simulated_provider(monkeypatch) -> None:
    db = _build_db_session()
    try:
        buyer, seller, _listing, transaction = _seed_transaction_context(
            db,
            serialized_listing=False,
            suffix="02",
        )
        provider = SimulatedCustodyProvider(
            db,
            collection_rail=SimulatedRail(),
            payout_rail=SimulatedRail(),
        )
        escrow_record = _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)

        confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
        dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
        monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "654321")
        mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
        withhold_buyer_delivery_otp(db, transaction_id=transaction.id, buyer=buyer)

        refund_result = provider.refund(
            PayoutRequest(
                escrow_reference=escrow_record.escrow_reference,
                amount=transaction.amount,
                destination_phone=buyer.mpesa_phone or buyer.phone,
                purpose=f"tx-refund:{transaction.id}",
            )
        )

        payout_attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        status = provider.get_status(escrow_record.escrow_reference)

        assert refund_result.outcome == PayoutOutcome.SUCCEEDED
        assert transaction.status == TransactionStatus.REFUNDED_BUYER
        assert len(payout_attempts) == 1
        assert payout_attempts[0].purpose == f"tx-refund:{transaction.id}"
        assert payout_attempts[0].outcome == AttemptOutcome.SUCCEEDED
        assert status.funded_amount == transaction.amount
        assert status.released_amount == Decimal("0.00")
        assert status.refunded_amount == transaction.amount
    finally:
        db.close()


@pytest.mark.parametrize(
    (
        "decision",
        "expected_tx_status",
        "expected_dispute_status",
        "expected_intents",
        "expected_released",
        "expected_refunded",
    ),
    [
        (
            AdminDecision.REFUND,
            TransactionStatus.RESOLVED_REFUND,
            DisputeStatus.RESOLVED_REFUND,
            ["admin-refund"],
            Decimal("0.00"),
            Decimal("2400.00"),
        ),
        (
            AdminDecision.RELEASE,
            TransactionStatus.RESOLVED_RELEASE,
            DisputeStatus.RESOLVED_RELEASE,
            ["admin-release"],
            Decimal("2400.00"),
            Decimal("0.00"),
        ),
        (
            AdminDecision.SPLIT,
            TransactionStatus.RESOLVED_SPLIT,
            DisputeStatus.RESOLVED_SPLIT,
            ["admin-split-release", "admin-split-refund"],
            Decimal("1200.00"),
            Decimal("1200.00"),
        ),
    ],
)
def test_dispute_escalation_admin_resolution_produces_expected_payout_intents(
    monkeypatch,
    decision: AdminDecision,
    expected_tx_status: TransactionStatus,
    expected_dispute_status: DisputeStatus,
    expected_intents: list[str],
    expected_released: Decimal,
    expected_refunded: Decimal,
) -> None:
    db = _build_db_session()
    try:
        buyer, seller, _listing, transaction = _seed_transaction_context(
            db,
            serialized_listing=True,
            suffix="03",
        )
        provider = SimulatedCustodyProvider(
            db,
            collection_rail=SimulatedRail(),
            payout_rail=SimulatedRail(),
        )
        escrow_record = _open_and_fund_escrow(db, provider, transaction=transaction, buyer=buyer)

        confirm_payment_callback(db, payload=_build_callback_payload(transaction.id))
        dispatch_transaction(db, transaction_id=transaction.id, seller=seller)
        monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "111222")
        mark_transaction_arrived(db, transaction_id=transaction.id, seller=seller)
        confirm_buyer_delivery_otp(
            db,
            transaction_id=transaction.id,
            buyer=buyer,
            otp_code="111222",
        )
        assert transaction.status == TransactionStatus.HOLD_24H

        report_functional_issue(
            db,
            transaction_id=transaction.id,
            buyer=buyer,
            category="not_working",
            description="Device no longer powers on.",
            evidence={"video": "https://example.com/evidence.mp4"},
        )
        mark_buyer_sent_back(db, transaction_id=transaction.id, buyer=buyer)
        mark_seller_received(db, transaction_id=transaction.id, seller=seller)
        apply_seller_resolution_action(
            db,
            transaction_id=transaction.id,
            seller=seller,
            action="repair_shipped",
            notes="Repair kit shipped.",
        )
        submit_buyer_reconfirmation(
            db,
            transaction_id=transaction.id,
            buyer=buyer,
            accepted=False,
            notes="Still failing after repair.",
        )
        apply_seller_resolution_action(
            db,
            transaction_id=transaction.id,
            seller=seller,
            action="replacement_shipped",
            notes="Replacement device shipped.",
        )
        submit_buyer_reconfirmation(
            db,
            transaction_id=transaction.id,
            buyer=buyer,
            accepted=False,
            notes="Second rejection.",
        )

        dispute = (
            db.execute(
                select(Dispute)
                .where(Dispute.transaction_id == transaction.id)
                .order_by(Dispute.created_at.desc())
            )
            .scalars()
            .first()
        )
        assert dispute is not None
        assert dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
        assert transaction.status == TransactionStatus.ESCALATED_ADMIN_REVIEW

        force_resolve_dispute_case(
            db,
            dispute.id,
            decision=decision,
            reason="Admin validated evidence.",
        )

        if decision == AdminDecision.REFUND:
            provider.refund(
                PayoutRequest(
                    escrow_reference=escrow_record.escrow_reference,
                    amount=transaction.amount,
                    destination_phone=buyer.mpesa_phone or buyer.phone,
                    purpose=f"admin-refund:{dispute.id}",
                )
            )
        elif decision == AdminDecision.RELEASE:
            provider.release(
                PayoutRequest(
                    escrow_reference=escrow_record.escrow_reference,
                    amount=transaction.amount,
                    destination_phone=seller.mpesa_phone or seller.phone,
                    purpose=f"admin-release:{dispute.id}",
                )
            )
        else:
            half = (transaction.amount / Decimal("2")).quantize(Decimal("0.01"))
            provider.release(
                PayoutRequest(
                    escrow_reference=escrow_record.escrow_reference,
                    amount=half,
                    destination_phone=seller.mpesa_phone or seller.phone,
                    purpose=f"admin-split-release:{dispute.id}",
                )
            )
            provider.refund(
                PayoutRequest(
                    escrow_reference=escrow_record.escrow_reference,
                    amount=transaction.amount - half,
                    destination_phone=buyer.mpesa_phone or buyer.phone,
                    purpose=f"admin-split-refund:{dispute.id}",
                )
            )

        payout_attempts = list(db.execute(select(PayoutAttempt)).scalars().all())
        status = provider.get_status(escrow_record.escrow_reference)

        assert transaction.status == expected_tx_status
        assert dispute.status == expected_dispute_status
        assert all(attempt.outcome == AttemptOutcome.SUCCEEDED for attempt in payout_attempts)
        for expected_prefix in expected_intents:
            assert any(
                attempt.purpose.startswith(f"{expected_prefix}:{dispute.id}")
                for attempt in payout_attempts
            )
        assert status.funded_amount == transaction.amount
        assert status.released_amount == expected_released
        assert status.refunded_amount == expected_refunded
    finally:
        db.close()
