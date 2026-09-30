import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.collection_attempt import AttemptOutcome, CollectionAttempt
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import simulated_custody_admin as simulated_admin_router
from app.services.auth import ActiveSessionContext
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.simulated_admin import (
    SimulatedCollectionAdminResult,
    SimulatedPayoutAdminResult,
    force_complete_simulated_collection,
    progress_simulated_collection_scenario,
    progress_simulated_payout_scenario,
)
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _build_user(role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} User",
        phone="+254700123456",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_session(user_id: uuid.UUID) -> UserSession:
    settings = get_settings()
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _authenticated_client(monkeypatch, role: UserRole) -> TestClient:
    user = _build_user(role)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    return client


def _csrf_headers() -> dict[str, str]:
    settings = get_settings()
    return {settings.csrf_header_name: "csrf-token"}


def _install_dummy_db_override() -> None:
    def fake_get_db():
        yield object()

    app.dependency_overrides[get_db] = fake_get_db


def _clear_dummy_db_override() -> None:
    app.dependency_overrides.pop(get_db, None)


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

    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return SessionLocal()


def test_force_complete_requires_authentication(monkeypatch) -> None:
    _install_dummy_db_override()
    try:
        monkeypatch.setattr(
            simulated_admin_router,
            "force_complete_simulated_collection",
            lambda *args, **kwargs: None,
        )
        client = TestClient(app)
        response = client.post(
            "/admin/simulated-custody/collections/force-complete",
            json={
                "escrow_reference": "sim-escrow-00000000-0000-0000-0000-000000000001",
                "amount": "100.00",
                "phone_number": "+254712345678",
            },
        )
    finally:
        _clear_dummy_db_override()

    assert response.status_code == 401


def test_force_complete_rejects_non_admin(monkeypatch) -> None:
    _install_dummy_db_override()
    try:
        monkeypatch.setattr(
            simulated_admin_router,
            "force_complete_simulated_collection",
            lambda *args, **kwargs: None,
        )
        client = _authenticated_client(monkeypatch, UserRole.BUYER)
        response = client.post(
            "/admin/simulated-custody/collections/force-complete",
            json={
                "escrow_reference": "sim-escrow-00000000-0000-0000-0000-000000000001",
                "amount": "100.00",
                "phone_number": "+254712345678",
            },
            headers=_csrf_headers(),
        )
    finally:
        _clear_dummy_db_override()

    assert response.status_code == 403


def test_force_complete_is_disabled_in_production(monkeypatch) -> None:
    _install_dummy_db_override()
    try:
        monkeypatch.setattr(
            simulated_admin_router,
            "get_settings",
            lambda: SimpleNamespace(environment="production"),
        )
        client = _authenticated_client(monkeypatch, UserRole.ADMIN)
        response = client.post(
            "/admin/simulated-custody/collections/force-complete",
            json={
                "escrow_reference": "sim-escrow-00000000-0000-0000-0000-000000000001",
                "amount": "100.00",
                "phone_number": "+254712345678",
            },
            headers=_csrf_headers(),
        )
    finally:
        _clear_dummy_db_override()

    assert response.status_code == 403
    assert response.json()["detail"] == "Simulation admin controls are disabled in production."


def test_force_complete_returns_result_payload_for_admin(monkeypatch) -> None:
    _install_dummy_db_override()
    try:
        result = SimulatedCollectionAdminResult(
            escrow_reference="sim-escrow-00000000-0000-0000-0000-000000000001",
            provider_reference="sim:collect:success:abc123",
            outcome=CollectionOutcome.SUCCEEDED,
            raw_status="accepted",
            message="forced success",
            idempotent_replay=False,
        )
        monkeypatch.setattr(
            simulated_admin_router,
            "force_complete_simulated_collection",
            lambda *args, **kwargs: result,
        )

        client = _authenticated_client(monkeypatch, UserRole.ADMIN)
        response = client.post(
            "/admin/simulated-custody/collections/force-complete",
            json={
                "escrow_reference": result.escrow_reference,
                "amount": "100.00",
                "phone_number": "+254712345678",
            },
            headers=_csrf_headers(),
        )
    finally:
        _clear_dummy_db_override()

    assert response.status_code == 200
    body = response.json()
    assert body["provider_reference"] == result.provider_reference
    assert body["outcome"] == CollectionOutcome.SUCCEEDED.value
    assert body["idempotent_replay"] is False


def test_force_complete_collection_is_idempotent_and_auditable() -> None:
    db = _build_db_session()
    try:
        provider = SimulatedCustodyProvider(
            db,
            collection_rail=SimulatedRail(),
            payout_rail=SimulatedRail(),
        )
        escrow = provider.open_escrow(
            OpenEscrowRequest(
                transaction_id=str(uuid.uuid4()),
                buyer_id=str(uuid.uuid4()),
                seller_id=str(uuid.uuid4()),
                amount=Decimal("600.00"),
            )
        )

        first = force_complete_simulated_collection(
            db,
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("600.00"),
            phone_number="+254712345678",
            account_reference="issue-70-demo",
        )
        second = force_complete_simulated_collection(
            db,
            escrow_reference=escrow.escrow_reference,
            amount=Decimal("600.00"),
            phone_number="+254712345678",
            account_reference="issue-70-demo",
        )

        attempts = list(db.execute(select(CollectionAttempt)).scalars().all())
        funding_entries = list(
            db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key.like("sim-funding:%")))
            .scalars()
            .all()
        )
        status = provider.get_status(escrow.escrow_reference)

        assert first.outcome == CollectionOutcome.SUCCEEDED
        assert first.idempotent_replay is False
        assert second.idempotent_replay is True
        assert len(attempts) == 1
        assert attempts[0].outcome == AttemptOutcome.SUCCEEDED
        assert attempts[0].request_snapshot["account_reference"].startswith("sim:success")
        assert len(funding_entries) == 2
        assert status.funded_amount == Decimal("600.00")
    finally:
        db.close()


def test_progress_collection_scenario_reconciles_out_of_order_idempotently() -> None:
    db = _build_db_session()
    try:
        rail = SimulatedRail()
        provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)
        escrow = provider.open_escrow(
            OpenEscrowRequest(
                transaction_id=str(uuid.uuid4()),
                buyer_id=str(uuid.uuid4()),
                seller_id=str(uuid.uuid4()),
                amount=Decimal("450.00"),
            )
        )

        initial = provider.request_funding(
            FundingRequest(
                escrow_reference=escrow.escrow_reference,
                amount=Decimal("450.00"),
                phone_number="+254712345678",
                account_reference="sim:out_of_order",
            )
        )

        first = progress_simulated_collection_scenario(
            db,
            provider_reference=initial.provider_reference,
        )
        second = progress_simulated_collection_scenario(
            db,
            provider_reference=initial.provider_reference,
        )
        third = progress_simulated_collection_scenario(
            db,
            provider_reference=initial.provider_reference,
        )

        attempt = db.execute(
            select(CollectionAttempt).where(
                CollectionAttempt.provider_reference == initial.provider_reference
            )
        ).scalar_one()
        funding_entries = list(
            db.execute(
                select(LedgerEntry).where(
                    LedgerEntry.idempotency_key == f"sim-funding:{initial.provider_reference}"
                )
            )
            .scalars()
            .all()
        )
        status = provider.get_status(escrow.escrow_reference)

        assert first.outcome == CollectionOutcome.UNKNOWN
        assert second.outcome == CollectionOutcome.SUCCEEDED
        assert third.idempotent_replay is True
        assert attempt.outcome == AttemptOutcome.SUCCEEDED
        assert attempt.response_snapshot["raw_status"] == "settled_after_out_of_order"
        assert len(funding_entries) == 2
        assert status.funded_amount == Decimal("450.00")
    finally:
        db.close()


def test_progress_payout_scenario_reconciles_out_of_order_idempotently() -> None:
    db = _build_db_session()
    try:
        rail = SimulatedRail()
        provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)
        escrow = provider.open_escrow(
            OpenEscrowRequest(
                transaction_id=str(uuid.uuid4()),
                buyer_id=str(uuid.uuid4()),
                seller_id=str(uuid.uuid4()),
                amount=Decimal("700.00"),
            )
        )
        provider.request_funding(
            FundingRequest(
                escrow_reference=escrow.escrow_reference,
                amount=Decimal("700.00"),
                phone_number="+254712345678",
                account_reference="fund-70",
            )
        )
        initial = provider.release(
            PayoutRequest(
                escrow_reference=escrow.escrow_reference,
                amount=Decimal("200.00"),
                destination_phone="+254712345678",
                purpose="sim:out_of_order",
            )
        )

        first = progress_simulated_payout_scenario(
            db,
            provider_reference=initial.provider_reference,
        )
        second = progress_simulated_payout_scenario(
            db,
            provider_reference=initial.provider_reference,
        )
        third = progress_simulated_payout_scenario(
            db,
            provider_reference=initial.provider_reference,
        )

        attempt = db.execute(
            select(PayoutAttempt).where(
                PayoutAttempt.provider_reference == initial.provider_reference
            )
        ).scalar_one()
        release_entries = list(
            db.execute(
                select(LedgerEntry).where(
                    LedgerEntry.idempotency_key == f"sim-release:{initial.provider_reference}"
                )
            )
            .scalars()
            .all()
        )
        status = provider.get_status(escrow.escrow_reference)

        assert first.outcome == PayoutOutcome.UNKNOWN
        assert second.outcome == PayoutOutcome.SUCCEEDED
        assert third.idempotent_replay is True
        assert attempt.outcome == AttemptOutcome.SUCCEEDED
        assert attempt.response_snapshot["raw_status"] == "settled_after_out_of_order"
        assert len(release_entries) == 2
        assert status.released_amount == Decimal("200.00")
    finally:
        db.close()


def test_progress_payout_endpoint_returns_payload(monkeypatch) -> None:
    _install_dummy_db_override()
    try:
        result = SimulatedPayoutAdminResult(
            escrow_reference="sim-escrow-00000000-0000-0000-0000-000000000001",
            provider_reference="sim:payout:success:abc123",
            payout_type="release",
            outcome=PayoutOutcome.SUCCEEDED,
            raw_status="settled",
            message="progressed",
            idempotent_replay=True,
        )
        monkeypatch.setattr(
            simulated_admin_router,
            "progress_simulated_payout_scenario",
            lambda *args, **kwargs: result,
        )

        client = _authenticated_client(monkeypatch, UserRole.ADMIN)
        response = client.post(
            "/admin/simulated-custody/payouts/sim:payout:success:abc123/progress",
            headers=_csrf_headers(),
        )
    finally:
        _clear_dummy_db_override()

    assert response.status_code == 200
    body = response.json()
    assert body["payout_type"] == "release"
    assert body["outcome"] == PayoutOutcome.SUCCEEDED.value
    assert body["idempotent_replay"] is True
