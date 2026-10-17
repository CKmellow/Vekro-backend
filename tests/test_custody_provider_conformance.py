from __future__ import annotations

from decimal import Decimal

from app.db.base import Base
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction
from app.services.custody.econfirm_provider import EconfirmCustodyProvider, EconfirmHttpResponse
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from tests.custody_provider_conformance import ProviderConformanceVector, run_provider_conformance


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

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return session_factory()


def _econfirm_envelope(data: dict, *, status_code: int = 200) -> EconfirmHttpResponse:
    return EconfirmHttpResponse(
        status_code=status_code,
        body={"success": True, "message": "ok", "data": data},
        text="{}",
    )


class _EconfirmRealProtocolTransport:
    """Mocks the real v1 endpoints for the eConfirm protocol-shape check.

    The shared vector-driven ``run_provider_conformance`` differentiates funding
    outcomes via ``account_reference``, which the real ``/payments/stk-push`` does
    not carry, so full shared-contract conformance for eConfirm is completed in
    E6 (where the harness is made transport-agnostic). Here we assert the adapter
    satisfies the CustodyProvider protocol on the real endpoints.
    """

    def __call__(self, method, url, headers, payload, timeout_seconds):
        _ = headers, timeout_seconds, payload
        if method == "POST" and url.endswith("/transactions"):
            return _econfirm_envelope(
                {"id": "txn_conf", "status": "pending", "confirmation_code": "AB12CD34"},
                status_code=201,
            )
        if method == "POST" and url.endswith("/payments/stk-push"):
            return _econfirm_envelope({"id": "txn_conf", "status": "stk_initiated"})
        if method == "POST" and url.endswith("/release"):
            return _econfirm_envelope({"id": "txn_conf", "status": "Completed"})
        if method == "POST" and url.endswith("/reversal"):
            return _econfirm_envelope({"id": "txn_conf", "status": "Completed"})
        if method == "GET" and "/transactions/" in url:
            return _econfirm_envelope({"id": "txn_conf", "status": "Escrow Funded", "amount": 500})
        return _econfirm_envelope({"id": "txn_conf", "status": "pending"})


def test_simulated_provider_conforms_to_shared_contract() -> None:
    db = _build_db_session()
    try:
        provider = SimulatedCustodyProvider(
            db,
            collection_rail=SimulatedRail(),
            payout_rail=SimulatedRail(),
        )
        run_provider_conformance(
            provider,
            vector=ProviderConformanceVector(
                success_funding_reference="sim:success conformance-fund-success",
                unknown_funding_reference="sim:timeout conformance-fund-timeout",
                failed_funding_reference="sim:failed conformance-fund-failed",
                success_release_purpose="sim:success conformance-release-success",
                idempotent_release_purpose="sim:duplicate conformance-release-idempotent",
                unknown_release_purpose="sim:timeout conformance-release-timeout",
                failed_refund_purpose="sim:failed conformance-refund-failed",
            ),
        )
    finally:
        db.close()


def test_econfirm_provider_satisfies_custody_protocol_on_real_endpoints() -> None:

    from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
    from app.services.custody.enums import CollectionOutcome, PayoutOutcome
    from app.services.custody.ports import CustodyProvider

    provider = EconfirmCustodyProvider(
        base_url="https://api.econfirm.co.ke/api/v1",
        api_key="ek_test",
        transport=_EconfirmRealProtocolTransport(),
        fallback_email="ops@vekro.example",
    )
    assert isinstance(provider, CustodyProvider)

    record = provider.open_escrow(
        OpenEscrowRequest(
            transaction_id="VK-CONF",
            buyer_id="buyer",
            seller_id="seller",
            amount=Decimal("500.00"),
            currency="KES",
            receiver_phone="+254700111333",
        )
    )
    assert record.escrow_reference == "txn_conf"
    assert record.confirmation_code == "AB12CD34"

    # STK push = prompt sent, not funded -> UNKNOWN; GET shows funded -> SUCCEEDED.
    funding = provider.request_funding(
        FundingRequest(
            escrow_reference=record.escrow_reference,
            amount=Decimal("500.00"),
            phone_number="+254700111222",
            account_reference="VK-CONF",
        )
    )
    assert funding.outcome == CollectionOutcome.UNKNOWN
    funding_status = provider.get_funding_status(record.escrow_reference)
    assert funding_status.outcome == CollectionOutcome.SUCCEEDED

    status = provider.get_status(record.escrow_reference)
    assert status.funded_amount == Decimal("500.00")

    release = provider.release(
        PayoutRequest(
            escrow_reference=record.escrow_reference,
            amount=Decimal("500.00"),
            destination_phone="+254700111333",
            purpose="release",
            confirmation_code="AB12CD34",
        )
    )
    refund = provider.refund(
        PayoutRequest(
            escrow_reference=record.escrow_reference,
            amount=Decimal("500.00"),
            destination_phone="+254700111222",
            purpose="buyer-refund",
            confirmation_code="AB12CD34",
        )
    )
    assert release.outcome == PayoutOutcome.SUCCEEDED
    assert refund.outcome == PayoutOutcome.SUCCEEDED
