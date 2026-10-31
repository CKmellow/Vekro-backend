"""E7: eConfirm wired into the live transaction funding flow.

Covers the three routes at transaction creation (eConfirm, honest simulated
fallback, Daraja default), poller-only funding confirmation, and the
custody_provider field on the API response.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from app.db.base import Base
from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.listing import Listing
from app.models.money_audit_event import MoneyAuditEvent
from app.models.notification import Notification, NotificationEventType
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import (
    CreateTransactionRequest,
    PaymentCallbackRequest,
    TransactionResponse,
)
from app.services import transaction as transaction_service
from app.services.custody.dto import CollectionResult, EscrowRecord
from app.services.custody.econfirm_crypto import build_cipher
from app.services.custody.econfirm_orchestration import run_econfirm_funding_reconciliation
from app.services.custody.econfirm_provider import EconfirmDefiniteError, EconfirmUnknownError
from app.services.custody.enums import CollectionOutcome
from app.services.payment import PaymentResponse
from app.services.transaction import (
    PaymentInitiationError,
    confirm_payment_callback,
    create_transaction,
)
from cryptography.fernet import Fernet
from sqlalchemy import Table, create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

KEY = Fernet.generate_key().decode()


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "econfirm_enabled": True,
        "econfirm_api_key": "test-key",
        "econfirm_code_encryption_key": KEY,
        "econfirm_min_amount": Decimal("100"),
        "econfirm_max_amount": Decimal("1000"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    json_columns = [
        cast(Any, Listing.__table__.c.dispute_policy),
        cast(Any, Notification.__table__.c.payload),
        cast(Any, MoneyAuditEvent.__table__.c.details),
    ]
    original = {column: column.server_default for column in json_columns}
    for column in json_columns:
        column.server_default = text("'{}'")
    tables = [
        cast(Table, model.__table__)
        for model in (
            User,
            Listing,
            Transaction,
            Notification,
            Escrow,
            LedgerEntry,
            MoneyAuditEvent,
            PaymentPrompt,
            AnchorEvent,
        )
    ]
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in original.items():
            column.server_default = default
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _user(role: UserRole, phone: str, *, mpesa_phone: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value} user",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=mpesa_phone,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
    )


def _seed(db: Session, *, price: str = "500.00") -> tuple[User, User, Listing]:
    buyer = _user(UserRole.BUYER, "+254711000001", mpesa_phone="0722000002")
    seller = _user(UserRole.SELLER, "+254733000003")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Phone",
        price=Decimal(price),
        is_serialized=False,
        dispute_policy={},
    )
    db.add_all([buyer, seller, listing])
    db.commit()
    return buyer, seller, listing


class _Provider:
    def __init__(self, *, open_exc=None, stk_outcome=CollectionOutcome.UNKNOWN):
        self.open_exc = open_exc
        self.stk_outcome = stk_outcome
        self.open_calls: list[Any] = []
        self.funding_calls: list[Any] = []

    def open_escrow(self, request):
        self.open_calls.append(request)
        if self.open_exc is not None:
            raise self.open_exc
        return EscrowRecord(
            escrow_reference="txn_E7LIVE",
            transaction_id=request.transaction_id,
            amount=request.amount,
            currency=request.currency,
            confirmation_code="CODE1234",
            status="pending",
        )

    def request_funding(self, request):
        self.funding_calls.append(request)
        return CollectionResult(
            outcome=self.stk_outcome,
            provider_reference=request.escrow_reference,
            raw_status="pending" if self.stk_outcome == CollectionOutcome.UNKNOWN else "rejected",
        )


class _Gateway:
    def __init__(self):
        self.calls: list[Any] = []

    def initiate_stk_push(self, request):
        self.calls.append(request)
        return PaymentResponse(
            provider="mpesa_stub",
            status="queued",
            provider_request_id="r",
            merchant_request_id="m",
            checkout_request_id="c",
            customer_message="ok",
        )


class _ExplodingProvider:
    def __getattr__(self, name):
        raise AssertionError(f"eConfirm provider must not be used ({name})")


def _escrow_for(db: Session, transaction: Transaction) -> Escrow | None:
    return db.execute(
        select(Escrow).where(Escrow.transaction_id == transaction.id)
    ).scalar_one_or_none()


def _create(db, buyer, listing, *, settings, provider, gateway):
    return create_transaction(
        db,
        buyer=buyer,
        payload=CreateTransactionRequest(listing_id=listing.id),
        payment_gateway=gateway,
        settings=settings,
        econfirm_provider=provider,
    )


# -- route 1: eConfirm ------------------------------------------------------------
def test_econfirm_route_opens_escrow_and_sends_econfirm_stk_only():
    db = _session()
    buyer, seller, listing = _seed(db)
    provider, gateway = _Provider(), _Gateway()

    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway
    )

    assert transaction.custody_provider == "econfirm"
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    # The Daraja gateway is never used for an eConfirm transaction.
    assert gateway.calls == []

    assert len(provider.open_calls) == 1
    assert provider.open_calls[0].receiver_phone == seller.phone
    assert len(provider.funding_calls) == 1
    stk = provider.funding_calls[0]
    assert stk.escrow_reference == "txn_E7LIVE"
    assert stk.phone_number == "+254722000002"  # buyer M-Pesa phone, normalized
    assert stk.account_reference == transaction.reference

    escrow = _escrow_for(db, transaction)
    assert escrow is not None
    assert escrow.custody_provider == "econfirm"
    assert escrow.econfirm_transaction_id == "txn_E7LIVE"
    assert escrow.econfirm_needs_reconciliation is False
    assert escrow.econfirm_payer_phone == "+254722000002"
    assert escrow.funded_amount == Decimal("0.00")  # a sent prompt is not funding
    assert escrow.econfirm_confirmation_code != "CODE1234"
    assert build_cipher(KEY).decrypt(escrow.econfirm_confirmation_code) == "CODE1234"


def test_econfirm_funding_is_confirmed_by_the_poller_and_locks_transaction():
    db = _session()
    buyer, _seller, listing = _seed(db)
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=_Provider(), gateway=_Gateway()
    )

    class _Rail:
        def get_funding_status(self, reference):
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED,
                provider_reference=reference,
                raw_status="funded",
            )

    result = run_econfirm_funding_reconciliation(db, collection_rail=_Rail())

    assert result.funded == 1
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert transaction.locked_at is not None
    assert _escrow_for(db, transaction).funded_amount == Decimal("500.00")
    locked = (
        db.execute(
            select(Notification).where(
                Notification.transaction_id == transaction.id,
                Notification.event_type == NotificationEventType.TRANSACTION_LOCKED,
            )
        )
        .scalars()
        .all()
    )
    assert {n.user_id for n in locked} == {transaction.buyer_id, transaction.seller_id}

    # A second run neither re-polls nor re-locks.
    assert run_econfirm_funding_reconciliation(db, collection_rail=_Rail()).polled == 0


def test_poller_leaves_transaction_awaiting_payment_while_unfunded():
    db = _session()
    buyer, _seller, listing = _seed(db)
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=_Provider(), gateway=_Gateway()
    )

    class _PendingRail:
        def get_funding_status(self, reference):
            return CollectionResult(
                outcome=CollectionOutcome.UNKNOWN,
                provider_reference=reference,
                raw_status="pending",
            )

    result = run_econfirm_funding_reconciliation(db, collection_rail=_PendingRail())

    assert (result.polled, result.funded) == (1, 0)
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT


def test_daraja_callback_cannot_lock_an_econfirm_transaction():
    db = _session()
    buyer, _seller, listing = _seed(db)
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=_Provider(), gateway=_Gateway()
    )

    result = confirm_payment_callback(
        db, payload=PaymentCallbackRequest(transaction_id=transaction.id, result_code=0)
    )

    assert result.transitioned is False
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT


def test_open_unknown_withholds_stk_and_flags_reconciliation():
    db = _session()
    buyer, _seller, listing = _seed(db)
    provider, gateway = _Provider(open_exc=EconfirmUnknownError("timeout")), _Gateway()

    with pytest.raises(PaymentInitiationError) as excinfo:
        _create(db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway)

    transaction = excinfo.value.transaction
    assert transaction is not None
    assert transaction.custody_provider == "econfirm"
    assert provider.funding_calls == []
    assert gateway.calls == []  # never falls back once eConfirm may hold the escrow
    escrow = _escrow_for(db, transaction)
    assert escrow.econfirm_needs_reconciliation is True
    assert escrow.econfirm_transaction_id is None


def test_stk_rejection_after_open_does_not_fall_back_to_simulated():
    db = _session()
    buyer, _seller, listing = _seed(db)
    provider = _Provider(stk_outcome=CollectionOutcome.FAILED_DEFINITE)
    gateway = _Gateway()

    with pytest.raises(PaymentInitiationError) as excinfo:
        _create(db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway)

    transaction = excinfo.value.transaction
    assert transaction.custody_provider == "econfirm"
    assert gateway.calls == []
    assert _escrow_for(db, transaction).custody_provider == "econfirm"


# -- route 2: honest simulated fallback -------------------------------------------
def test_definite_open_rejection_falls_back_to_simulated_and_existing_path():
    db = _session()
    buyer, _seller, listing = _seed(db)
    provider = _Provider(open_exc=EconfirmDefiniteError("bad request"))
    gateway = _Gateway()

    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway
    )

    assert transaction.custody_provider == "simulated"
    assert provider.funding_calls == []
    assert len(gateway.calls) == 1
    assert _escrow_for(db, transaction).custody_provider == "simulated"


@pytest.mark.parametrize(
    ("price", "settings_overrides", "breaker_open"),
    [
        ("5000.00", {}, False),  # above ECONFIRM_MAX_AMOUNT
        ("50.00", {}, False),  # below ECONFIRM_MIN_AMOUNT
        ("500.00", {"econfirm_code_encryption_key": ""}, False),
        ("500.00", {"econfirm_api_key": ""}, False),
        ("500.00", {}, True),
    ],
)
def test_selection_fallback_uses_existing_path_without_touching_econfirm(
    monkeypatch, price, settings_overrides, breaker_open
):
    db = _session()
    buyer, _seller, listing = _seed(db, price=price)
    monkeypatch.setattr(
        transaction_service,
        "blocked_rails_for_routing",
        lambda _db, **_kw: {"econfirm"} if breaker_open else set(),
    )
    gateway = _Gateway()

    transaction = _create(
        db,
        buyer,
        listing,
        settings=_settings(**settings_overrides),
        provider=_ExplodingProvider(),
        gateway=gateway,
    )

    assert transaction.custody_provider == "simulated"
    assert len(gateway.calls) == 1
    assert _escrow_for(db, transaction) is None


# -- route 3: Daraja default (ECONFIRM_ENABLED=false) -----------------------------
def test_default_disabled_keeps_daraja_path_unchanged(monkeypatch):
    db = _session()
    buyer, _seller, listing = _seed(db)

    def _no_breaker_lookup(*_args, **_kwargs):
        raise AssertionError("rail health must not be consulted when eConfirm is disabled")

    monkeypatch.setattr(transaction_service, "blocked_rails_for_routing", _no_breaker_lookup)
    gateway = _Gateway()

    transaction = _create(
        db,
        buyer,
        listing,
        settings=_settings(econfirm_enabled=False),
        provider=_ExplodingProvider(),
        gateway=gateway,
    )

    assert transaction.custody_provider == "simulated"
    assert len(gateway.calls) == 1
    assert gateway.calls[0].phone_number == "+254722000002"
    assert _escrow_for(db, transaction) is None

    # The Daraja callback still locks non-eConfirm transactions.
    result = confirm_payment_callback(
        db, payload=PaymentCallbackRequest(transaction_id=transaction.id, result_code=0)
    )
    assert result.transitioned is True


# -- API contract ------------------------------------------------------------------
def _response_for(custody_provider: str | None) -> TransactionResponse:
    now = datetime.now(UTC)
    transaction = Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount=Decimal("500.00"),
        status=TransactionStatus.AWAITING_PAYMENT,
        created_at=now,
        updated_at=now,
    )
    transaction.custody_provider = custody_provider
    return TransactionResponse.model_validate(transaction)


def test_transaction_response_exposes_custody_provider():
    assert _response_for("econfirm").custody_provider == "econfirm"
    assert _response_for("simulated").custody_provider == "simulated"
    # Unset/unknown values are never presented as real custody.
    assert _response_for(None).custody_provider == "simulated"
    assert _response_for("loop").custody_provider == "simulated"


def test_transaction_custody_migration_is_additive():
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "e2b7c4d91f08_m20_transaction_custody_provider.py"
    )
    spec = importlib.util.spec_from_file_location("m20_txn_custody", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "b5e8f1c2a904"

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE transactions (id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO transactions (id) VALUES ('legacy-1')"))
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        conn.commit()
        value = conn.execute(text("SELECT custody_provider FROM transactions")).scalar_one()
    assert value == "simulated"


def test_transaction_detail_response_carries_custody_provider():
    from app.schemas.transaction_detail import TransactionDetailResponse
    from app.services.transaction_read import TransactionView

    db = _session()
    buyer, seller, listing = _seed(db)
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=_Provider(), gateway=_Gateway()
    )

    detail = TransactionDetailResponse.from_view(
        TransactionView(
            transaction=transaction,
            listing=listing,
            buyer=buyer,
            seller=seller,
            viewer_role=UserRole.BUYER,
        )
    )

    assert detail.custody_provider == "econfirm"


# -- payment prompts (instruction + resend) ------------------------------------------
def _prompts(db: Session, transaction: Transaction) -> list[PaymentPrompt]:
    return list(
        db.execute(
            select(PaymentPrompt)
            .where(PaymentPrompt.transaction_id == transaction.id)
            .order_by(PaymentPrompt.sent_at.asc())
        )
        .scalars()
        .all()
    )


def test_econfirm_create_records_pending_stk_prompt_and_poller_resolves_it():
    db = _session()
    buyer, _seller, listing = _seed(db)
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=_Provider(), gateway=_Gateway()
    )

    (prompt,) = _prompts(db, transaction)
    assert prompt.kind == "stk_prompt"
    assert prompt.status == "pending"
    assert prompt.checkout_request_id == "txn_E7LIVE"

    class _Rail:
        def get_funding_status(self, reference):
            return CollectionResult(
                outcome=CollectionOutcome.SUCCEEDED, provider_reference=reference, raw_status="f"
            )

    run_econfirm_funding_reconciliation(db, collection_rail=_Rail())
    db.refresh(prompt)
    assert prompt.status == "succeeded"
    assert prompt.resolved_at is not None


@pytest.mark.parametrize(
    "provider",
    [
        _Provider(stk_outcome=CollectionOutcome.FAILED_DEFINITE),
        _Provider(open_exc=EconfirmUnknownError("timeout")),
    ],
    ids=["stk_rejected", "open_unknown"],
)
def test_econfirm_initiation_failure_records_failed_prompt(provider):
    db = _session()
    buyer, _seller, listing = _seed(db)

    with pytest.raises(PaymentInitiationError) as excinfo:
        _create(db, buyer, listing, settings=_settings(), provider=provider, gateway=_Gateway())

    (prompt,) = _prompts(db, excinfo.value.transaction)
    assert prompt.status == "failed_to_initiate"


def test_resend_for_econfirm_goes_through_econfirm_not_daraja():
    from datetime import timedelta

    from app.services.payment_prompts import resend_payment_prompt

    db = _session()
    buyer, _seller, listing = _seed(db)
    provider, gateway = _Provider(), _Gateway()
    transaction = _create(
        db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway
    )

    prompt = resend_payment_prompt(
        db,
        transaction_id=transaction.id,
        buyer=buyer,
        payment_gateway=gateway,
        now=datetime.now(UTC) + timedelta(minutes=1),
        econfirm_provider=provider,
        settings=_settings(),
    )

    assert gateway.calls == []
    assert len(provider.funding_calls) == 2
    assert prompt.status == "pending"
    assert prompt.checkout_request_id == "txn_E7LIVE"
    first, second = _prompts(db, transaction)
    assert first.status == "failed"  # superseded


def test_resend_is_refused_while_econfirm_escrow_needs_reconciliation():
    from datetime import timedelta

    from app.services.payment_prompts import (
        PaymentResendInvalidStateError,
        resend_payment_prompt,
    )

    db = _session()
    buyer, _seller, listing = _seed(db)
    provider, gateway = _Provider(open_exc=EconfirmUnknownError("timeout")), _Gateway()
    with pytest.raises(PaymentInitiationError) as excinfo:
        _create(db, buyer, listing, settings=_settings(), provider=provider, gateway=gateway)

    with pytest.raises(PaymentResendInvalidStateError):
        resend_payment_prompt(
            db,
            transaction_id=excinfo.value.transaction.id,
            buyer=buyer,
            payment_gateway=gateway,
            now=datetime.now(UTC) + timedelta(minutes=1),
            econfirm_provider=provider,
            settings=_settings(),
        )
    assert gateway.calls == []
    assert provider.funding_calls == []
