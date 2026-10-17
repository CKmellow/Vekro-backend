"""E5: open-on-eConfirm persistence, the funding poller, and backoff jitter."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.db.base import Base
from app.models.escrow import Escrow
from app.services.custody.dto import CollectionResult, EscrowRecord
from app.services.custody.econfirm_crypto import build_cipher
from app.services.custody.econfirm_orchestration import (
    next_poll_delay_seconds,
    open_econfirm_escrow,
    run_econfirm_funding_reconciliation,
)
from app.services.custody.econfirm_provider import EconfirmDefiniteError, EconfirmUnknownError
from app.services.custody.enums import CollectionOutcome
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

KEY = Fernet.generate_key().decode()
SETTINGS = SimpleNamespace(econfirm_code_encryption_key=KEY, econfirm_max_amount=Decimal("1000"))


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Escrow.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _escrow(db, *, created_at=None) -> Escrow:
    escrow = Escrow(
        id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount_total=Decimal("500.00"),
        funded_amount=Decimal("0.00"),
        currency="KES",
    )
    if created_at is not None:
        escrow.created_at = created_at
    db.add(escrow)
    db.flush()
    return escrow


class _Provider:
    def __init__(self, *, record=None, exc=None):
        self._record = record
        self._exc = exc

    def open_escrow(self, request):
        if self._exc is not None:
            raise self._exc
        return self._record


class _CollectionRail:
    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls = []

    def get_funding_status(self, provider_reference):
        self.calls.append(provider_reference)
        return CollectionResult(
            outcome=self._outcomes.get(provider_reference, CollectionOutcome.UNKNOWN),
            provider_reference=provider_reference,
            raw_status="x",
        )


def test_open_success_persists_sticky_state_and_encrypts_code():
    db = _session()
    escrow = _escrow(db)
    record = EscrowRecord(
        escrow_reference="txn_LIVE1",
        transaction_id=str(escrow.transaction_id),
        amount=Decimal("500.00"),
        currency="KES",
        confirmation_code="AB12CD34",
        status="pending",
        max_principal_kes=Decimal("500000.00"),
    )
    provider = _Provider(record=record)

    result = open_econfirm_escrow(
        db,
        escrow,
        provider=provider,
        settings=SETTINGS,
        buyer_email="b@example.com",
        seller_email="s@example.com",
        receiver_phone="+254712345678",
    )
    db.commit()

    assert result == "econfirm"
    assert escrow.custody_provider == "econfirm"
    assert escrow.econfirm_transaction_id == "txn_LIVE1"
    assert escrow.econfirm_needs_reconciliation is False
    # Stored ciphertext is not the plaintext, but decrypts back to it.
    assert escrow.econfirm_confirmation_code != "AB12CD34"
    assert build_cipher(KEY).decrypt(escrow.econfirm_confirmation_code) == "AB12CD34"


def test_open_unknown_flags_reconciliation_and_sends_no_stk():
    db = _session()
    escrow = _escrow(db)
    provider = _Provider(exc=EconfirmUnknownError("timeout"))

    result = open_econfirm_escrow(
        db,
        escrow,
        provider=provider,
        settings=SETTINGS,
        buyer_email="b@example.com",
        seller_email="s@example.com",
        receiver_phone="+254712345678",
    )
    db.commit()

    assert result == "econfirm"
    assert escrow.custody_provider == "econfirm"
    assert escrow.econfirm_needs_reconciliation is True
    assert escrow.econfirm_transaction_id is None
    assert escrow.econfirm_confirmation_code is None


def test_open_definite_rejection_falls_back_to_simulated():
    db = _session()
    escrow = _escrow(db)
    provider = _Provider(exc=EconfirmDefiniteError("bad request"))

    result = open_econfirm_escrow(
        db,
        escrow,
        provider=provider,
        settings=SETTINGS,
        buyer_email="b@example.com",
        seller_email="s@example.com",
        receiver_phone="+254712345678",
    )
    db.commit()

    assert result == "simulated"
    assert escrow.custody_provider == "simulated"
    assert escrow.econfirm_transaction_id is None


def test_poller_confirms_funding_only_on_funded_status():
    db = _session()
    now = datetime.now(UTC)
    funded = _escrow(db, created_at=now)
    funded.custody_provider = "econfirm"
    funded.econfirm_transaction_id = "txn_FUND"
    pending = _escrow(db, created_at=now)
    pending.custody_provider = "econfirm"
    pending.econfirm_transaction_id = "txn_PEND"
    db.commit()

    rail = _CollectionRail({"txn_FUND": CollectionOutcome.SUCCEEDED})
    result = run_econfirm_funding_reconciliation(db, collection_rail=rail, now=now)

    assert result.polled == 2
    assert result.funded == 1
    db.refresh(funded)
    db.refresh(pending)
    assert funded.funded_amount == Decimal("500.00")  # confirmed
    assert pending.funded_amount == Decimal("0.00")  # still awaiting


def test_poller_skips_escrows_past_the_payment_timeout():
    db = _session()
    now = datetime.now(UTC)
    stale = _escrow(db, created_at=now - timedelta(minutes=30))
    stale.custody_provider = "econfirm"
    stale.econfirm_transaction_id = "txn_OLD"
    db.commit()

    rail = _CollectionRail({"txn_OLD": CollectionOutcome.SUCCEEDED})
    result = run_econfirm_funding_reconciliation(
        db, collection_rail=rail, now=now, payment_timeout_minutes=15
    )

    assert result.polled == 0
    assert result.skipped_timed_out == 1
    assert rail.calls == []  # never called the API for a timed-out escrow


def test_poller_ignores_simulated_and_needs_reconciliation():
    db = _session()
    now = datetime.now(UTC)
    sim = _escrow(db, created_at=now)
    sim.custody_provider = "simulated"
    needs = _escrow(db, created_at=now)
    needs.custody_provider = "econfirm"
    needs.econfirm_transaction_id = None
    needs.econfirm_needs_reconciliation = True
    db.commit()

    rail = _CollectionRail({})
    result = run_econfirm_funding_reconciliation(db, collection_rail=rail, now=now)
    assert result.polled == 0


def test_backoff_jitter_stays_within_bounds():
    rng = random.Random(1234)
    for attempt in range(6):
        delay = next_poll_delay_seconds(attempt, rng=rng)
        assert 10.0 <= delay <= 60.0
