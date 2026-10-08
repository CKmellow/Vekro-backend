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


class _EconfirmScenarioTransport:
    def __call__(self, method, url, headers, payload, timeout_seconds):
        _ = headers
        _ = timeout_seconds
        if method == "POST" and url.endswith("/v1/escrows/open"):
            return EconfirmHttpResponse(
                status_code=200,
                body={
                    "status": "accepted",
                    "escrow_reference": "ecf-escrow-conformance",
                },
                text="ok",
            )

        if method == "POST" and url.endswith("/fund"):
            account_reference = str((payload or {}).get("account_reference") or "")
            normalized_reference = account_reference.replace(" ", "-").lower()
            if "failed" in normalized_reference:
                status = "failed"
            elif "timeout" in normalized_reference:
                status = "pending"
            else:
                status = "succeeded"
            return EconfirmHttpResponse(
                status_code=200,
                body={
                    "status": status,
                    "provider_reference": f"ecf-fund-{normalized_reference}",
                    "message": "funding response",
                },
                text="ok",
            )

        if method == "POST" and (url.endswith("/releases") or url.endswith("/reversals")):
            purpose = str((payload or {}).get("purpose") or "")
            normalized_purpose = purpose.replace(" ", "-").lower()
            if "failed" in normalized_purpose:
                status = "failed"
            elif "timeout" in normalized_purpose:
                status = "pending"
            else:
                status = "succeeded"
            return EconfirmHttpResponse(
                status_code=200,
                body={
                    "status": status,
                    "provider_reference": f"ecf-pay-{normalized_purpose}",
                    "message": "payout response",
                },
                text="ok",
            )

        if method == "GET" and "/v1/escrows/" in url:
            return EconfirmHttpResponse(
                status_code=200,
                body={
                    "status": "succeeded",
                    "funded_amount": str(Decimal("500.00")),
                    "released_amount": str(Decimal("115.00")),
                    "refunded_amount": str(Decimal("0.00")),
                },
                text="ok",
            )

        return EconfirmHttpResponse(status_code=200, body={"status": "pending"}, text="ok")


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


def test_econfirm_provider_conforms_to_shared_contract() -> None:
    provider = EconfirmCustodyProvider(
        base_url="https://sandbox.econfirm.example",
        api_key="test-api-key",
        api_secret="test-api-secret",
        transport=_EconfirmScenarioTransport(),
    )

    run_provider_conformance(
        provider,
        vector=ProviderConformanceVector(
            success_funding_reference="conformance-fund-success",
            unknown_funding_reference="conformance-fund-timeout",
            failed_funding_reference="conformance-fund-failed",
            success_release_purpose="conformance-release-success",
            idempotent_release_purpose="conformance-release-idempotent",
            unknown_release_purpose="conformance-release-timeout",
            failed_refund_purpose="conformance-refund-failed",
        ),
    )