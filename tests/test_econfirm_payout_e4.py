"""E4: the payout executor decrypts and threads the eConfirm confirmation code.

Columns are set directly here (open-time persistence/routing is E5). A capturing
fake rail stands in for the eConfirm payout rail so we can assert exactly what the
executor sends, and that the ledger posts only after a confirmed success.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import app.services.escrow_service as escrow_service
from app.db.base import Base
from app.models.collection_attempt import AttemptOutcome
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction
from app.models.user import User
from app.services.custody.dto import PayoutResult
from app.services.custody.econfirm_crypto import build_cipher
from app.services.custody.enums import PayoutOutcome
from app.services.escrow_service import run_payout_executor_once
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


class _CapturingRail:
    def __init__(self, outcome: PayoutOutcome, raw_status: str) -> None:
        self.calls: list = []
        self._outcome = outcome
        self._raw_status = raw_status

    def request_payout(self, request):
        self.calls.append(request)
        return PayoutResult(
            outcome=self._outcome,
            provider_reference="ecf-pay-1",
            raw_status=self._raw_status,
            message=None,
        )

    def get_payout_status(self, provider_reference):  # pragma: no cover - not hit here
        return PayoutResult(
            outcome=self._outcome,
            provider_reference=provider_reference,
            raw_status=self._raw_status,
        )


class _FakeRegistry:
    def __init__(self, rail: _CapturingRail) -> None:
        self._rail = rail

    def get_payout_rail(self, rail_name):
        return self._rail


def _build_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Transaction.__table__,
            Escrow.__table__,
            LedgerEntry.__table__,
        ],
    )
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE payout_attempts (
                id TEXT PRIMARY KEY, escrow_id TEXT NOT NULL, purpose VARCHAR(64) NOT NULL,
                rail_name VARCHAR(40) NOT NULL, provider_name VARCHAR(40) NOT NULL,
                idempotency_key VARCHAR(120) NOT NULL, provider_reference VARCHAR(120),
                amount NUMERIC(12,2) NOT NULL, currency VARCHAR(3) NOT NULL DEFAULT 'KES',
                outcome VARCHAR(15) NOT NULL DEFAULT 'unknown',
                request_snapshot JSON NOT NULL DEFAULT '{}',
                response_snapshot JSON NOT NULL DEFAULT '{}',
                failure_code VARCHAR(64), failure_reason VARCHAR(255),
                attempted_at DATETIME NOT NULL, created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_payout_attempts_amount_positive CHECK (amount > 0)
            )
            """))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    return factory()


def _seed_funded_econfirm_escrow(db: Session, *, code_token: str | None) -> Escrow:
    txn_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO transactions (id, reference, listing_id, buyer_id, seller_id, amount, "
            "status, payout_status, otp_failed_attempts, created_at, updated_at) VALUES "
            "(:id, :ref, :lid, :bid, :sid, 500, 'hold_24h', 'pending', 0, :now, :now)"
        ),
        {
            "id": txn_id.hex,
            "ref": "VK-TEST01",
            "lid": uuid.uuid4().hex,
            "bid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "now": datetime.now(UTC),
        },
    )
    escrow = Escrow(
        id=uuid.uuid4(),
        transaction_id=txn_id,
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount_total=Decimal("500.00"),
        funded_amount=Decimal("500.00"),
        released_amount=Decimal("0.00"),
        refunded_amount=Decimal("0.00"),
        currency="KES",
        custody_provider="econfirm",
        econfirm_confirmation_code=code_token,
        econfirm_payer_phone="+254712345678",
    )
    db.add(escrow)
    db.flush()

    db.execute(
        text(
            "INSERT INTO payout_attempts (id, escrow_id, purpose, rail_name, provider_name, "
            "idempotency_key, provider_reference, amount, currency, outcome, request_snapshot, "
            "response_snapshot, attempted_at, created_at, updated_at) VALUES "
            "(:id, :eid, :purpose, 'econfirm', 'econfirm', :idem, NULL, 500, 'KES', 'unknown', "
            ":req, '{}', :now, :now, :now)"
        ),
        {
            "id": uuid.uuid4().hex,
            "eid": escrow.id.hex,
            "purpose": "tx-release:1",
            "idem": f"outbox:{uuid.uuid4().hex}",
            "req": (
                '{"payout_kind":"release",' '"destination_phone":"+254712345678","amount":"500.00"}'
            ),
            "now": datetime.now(UTC),
        },
    )
    db.commit()
    return escrow


def _patch_registry_and_key(monkeypatch, rail: _CapturingRail, key: str) -> None:
    monkeypatch.setattr(
        escrow_service, "build_custody_registry", lambda _settings: _FakeRegistry(rail)
    )
    monkeypatch.setattr(
        "app.core.settings.get_settings",
        lambda: SimpleNamespace(econfirm_code_encryption_key=key),
    )


def test_executor_decrypts_and_threads_code_then_posts_ledger(monkeypatch):
    key = Fernet.generate_key().decode()
    token = build_cipher(key).encrypt("AB12CD34")
    rail = _CapturingRail(PayoutOutcome.SUCCEEDED, "Completed")

    db = _build_session()
    try:
        escrow = _seed_funded_econfirm_escrow(db, code_token=token)
        _patch_registry_and_key(monkeypatch, rail, key)

        result = run_payout_executor_once(db)

        assert result.succeeded == 1
        assert len(rail.calls) == 1
        sent = rail.calls[0]
        assert sent.confirmation_code == "AB12CD34"  # decrypted and threaded
        assert sent.notes  # non-empty (A5)

        db.refresh(escrow)
        assert escrow.released_amount == Decimal("500.00")  # ledger posted after success
        ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.escrow_id == escrow.id).count()
        assert ledger_rows == 2
    finally:
        db.close()


def test_executor_leaves_attempt_unknown_when_code_cannot_be_decrypted(monkeypatch):
    # Token encrypted with a DIFFERENT key than the one the executor will use.
    wrong_token = build_cipher(Fernet.generate_key().decode()).encrypt("AB12CD34")
    rail = _CapturingRail(PayoutOutcome.SUCCEEDED, "Completed")

    db = _build_session()
    try:
        _seed_funded_econfirm_escrow(db, code_token=wrong_token)
        _patch_registry_and_key(monkeypatch, rail, Fernet.generate_key().decode())

        result = run_payout_executor_once(db)

        assert result.unknown == 1
        assert rail.calls == []  # never called the rail with a missing/garbage code
        attempt = db.execute(text("SELECT outcome, failure_code FROM payout_attempts")).fetchone()
        assert attempt[0] == AttemptOutcome.UNKNOWN.value
        assert attempt[1] == "confirmation_code_decrypt_failed"
    finally:
        db.close()
