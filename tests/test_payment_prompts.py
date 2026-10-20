import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.listing import Listing
from app.models.notification import Notification
from app.models.payment_prompt import PaymentFailureReason, PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.services import payment_prompts
from app.services.auth import ActiveSessionContext
from app.services.payment_prompts import (
    PROMPT_LIFETIME,
    RESEND_COOLDOWN,
    failure_reason_for,
    prompt_state,
)
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return "JSON"


NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _user(role: UserRole, phone: str, mpesa_phone: str | None = None) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value} user",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=mpesa_phone,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    json_columns = [
        cast(Any, Listing.__table__.c.dispute_policy),
        cast(Any, Notification.__table__.c.payload),
    ]
    originals = {column: column.server_default for column in json_columns}
    for column in json_columns:
        column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                cast(Table, User.__table__),
                cast(Table, Listing.__table__),
                cast(Table, Transaction.__table__),
                cast(Table, Notification.__table__),
                cast(Table, PaymentPrompt.__table__),
            ],
        )
    finally:
        for column, default in originals.items():
            column.server_default = default
    session = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def world(db: Session) -> dict[str, Any]:
    buyer = _user(UserRole.BUYER, "+254712345678")
    other_buyer = _user(UserRole.BUYER, "+254733000111")
    seller = _user(UserRole.SELLER, "+254722118904", "+254722118904")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Nike Air Force 1, size 42",
        price=Decimal("6500.00"),
        is_serialized=False,
        unique_id=None,
        dispute_policy={},
        created_at=NOW,
        updated_at=NOW,
    )
    db.add_all([buyer, other_buyer, seller, listing])
    db.commit()
    return {"buyer": buyer, "other_buyer": other_buyer, "seller": seller, "listing": listing}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _client_as(monkeypatch, db: Session, user: User) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        session_token_hash=hash_session_token("session-token", get_settings().secret_key),
        csrf_token_hash=hash_session_token("csrf-token", get_settings().secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    monkeypatch.setattr(
        app_main,
        "_load_session_context",
        lambda _token: ActiveSessionContext(user=user, session=session),
    )
    client = TestClient(app)
    client.cookies.set(get_settings().session_cookie_name, "session-token")
    client.cookies.set(get_settings().csrf_cookie_name, "csrf-token")
    client.headers[get_settings().csrf_header_name] = "csrf-token"
    return client


def _create(monkeypatch, db: Session, world: dict[str, Any]) -> dict[str, Any]:
    client = _client_as(monkeypatch, db, world["buyer"])
    response = client.post("/transactions", json={"listing_id": str(world["listing"].id)})
    assert response.status_code == 201
    return response.json()


def _callback(client: TestClient, transaction_id: str, result_code: int) -> None:
    response = client.post(
        "/transactions/payment-callback",
        json={"transaction_id": transaction_id, "result_code": result_code},
    )
    assert response.status_code in (200, 202)


def _age_prompts(db: Session, by: timedelta) -> None:
    for prompt in db.execute(select(PaymentPrompt)).scalars():
        prompt.sent_at = prompt.sent_at.replace(tzinfo=UTC) - by
        prompt.expires_at = prompt.expires_at.replace(tzinfo=UTC) - by
    db.commit()


def test_create_returns_an_stk_instruction(monkeypatch, db, world) -> None:
    body = _create(monkeypatch, db, world)

    payment = body["payment"]
    assert payment["kind"] == "STK_PROMPT"
    assert payment["redirect_url"] is None
    assert payment["status"] == "pending"
    assert payment["failure_reason"] is None
    sent_at = datetime.fromisoformat(payment["sent_at"])
    assert datetime.fromisoformat(payment["expires_at"]) - sent_at == PROMPT_LIFETIME
    assert datetime.fromisoformat(payment["resend_available_at"]) - sent_at == RESEND_COOLDOWN


def test_create_prompts_the_account_phone_when_no_mpesa_phone(monkeypatch, db, world) -> None:
    sent: list[str | None] = []

    class _Gateway:
        def initiate_stk_push(self, request):
            sent.append(request.phone_number)
            return payment_prompts.PaymentResponse(
                provider="fake",
                status="queued",
                provider_request_id="r",
                merchant_request_id="m",
                checkout_request_id="c",
                customer_message="ok",
            )

    monkeypatch.setattr("app.services.transaction.get_payment_gateway", lambda: _Gateway())
    _create(monkeypatch, db, world)

    assert sent == ["+254712345678"]


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (1032, "cancelled"),
        (2001, "wrong_pin"),
        (1, "insufficient_funds"),
        (1037, "timeout"),
        (17, "provider_error"),
    ],
)
def test_failed_callback_records_the_reason(monkeypatch, db, world, code, reason) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])

    _callback(client, created["id"], code)
    body = client.get(f"/transactions/{created['id']}").json()

    assert body["status"] == "awaiting_payment"
    assert body["payment"]["status"] == "failed"
    assert body["payment"]["failure_reason"] == reason


def test_successful_callback_closes_the_prompt(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])

    _callback(client, created["id"], 0)
    body = client.get(f"/transactions/{created['id']}").json()

    assert body["status"] == "locked"
    assert body["payment"]["status"] == "succeeded"


def test_seller_does_not_see_the_payment_prompt(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    seller = _client_as(monkeypatch, db, world["seller"])

    assert seller.get(f"/transactions/{created['id']}").json()["payment"] is None


def test_unanswered_prompt_reads_as_expired(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    _age_prompts(db, PROMPT_LIFETIME)
    client = _client_as(monkeypatch, db, world["buyer"])

    assert client.get(f"/transactions/{created['id']}").json()["payment"]["status"] == "expired"


def test_resend_waits_for_the_cooldown(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])

    response = client.post(f"/transactions/{created['id']}/payment/resend")

    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= RESEND_COOLDOWN.seconds + 1


def test_resend_sends_a_new_prompt_and_supersedes_the_old(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])
    _callback(client, created["id"], 1032)
    _age_prompts(db, RESEND_COOLDOWN)

    response = client.post(f"/transactions/{created['id']}/payment/resend")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["failure_reason"] is None
    prompts = db.execute(select(PaymentPrompt)).scalars().all()
    assert len(prompts) == 2


def test_resend_closes_a_still_open_prompt(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    _age_prompts(db, RESEND_COOLDOWN)
    client = _client_as(monkeypatch, db, world["buyer"])

    assert client.post(f"/transactions/{created['id']}/payment/resend").status_code == 200

    statuses = sorted(p.status for p in db.execute(select(PaymentPrompt)).scalars())
    assert statuses == ["failed", "pending"]


def test_resend_is_for_the_buyer_only(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    _age_prompts(db, RESEND_COOLDOWN)

    seller = _client_as(monkeypatch, db, world["seller"])
    assert seller.post(f"/transactions/{created['id']}/payment/resend").status_code == 403

    other = _client_as(monkeypatch, db, world["other_buyer"])
    assert other.post(f"/transactions/{created['id']}/payment/resend").status_code == 404


def test_resend_needs_an_unpaid_transaction(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])
    _callback(client, created["id"], 0)

    response = client.post(f"/transactions/{created['id']}/payment/resend")

    assert response.status_code == 422


def test_resend_reports_a_failed_send(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    _age_prompts(db, RESEND_COOLDOWN)

    class _Broken:
        def initiate_stk_push(self, _request):
            raise RuntimeError("transport down")

    monkeypatch.setattr(payment_prompts, "get_payment_gateway", lambda: _Broken())
    client = _client_as(monkeypatch, db, world["buyer"])

    response = client.post(f"/transactions/{created['id']}/payment/resend")

    assert response.status_code == 202
    assert response.json()["status"] == "failed_to_initiate"


def test_unknown_result_codes_are_provider_errors() -> None:
    assert failure_reason_for(999) == PaymentFailureReason.PROVIDER_ERROR


def test_prompt_state_is_pending_until_expiry() -> None:
    prompt = PaymentPrompt(
        id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        kind="stk_prompt",
        status="pending",
        sent_at=NOW,
        expires_at=NOW + PROMPT_LIFETIME,
    )

    assert prompt_state(prompt, now=NOW + PROMPT_LIFETIME / 2).status == "pending"
    assert prompt_state(prompt, now=NOW + PROMPT_LIFETIME).status == "expired"


def test_contract_lists_the_new_operations() -> None:
    paths = app.openapi()["paths"]
    created = paths["/transactions"]["post"]["responses"]["201"]["content"]["application/json"]
    resend = paths["/transactions/{transaction_id}/payment/resend"]["post"]

    assert created["schema"]["$ref"].endswith("/TransactionCreatedResponse")
    assert "429" in resend["responses"]


def test_late_callback_for_a_locked_transaction_changes_nothing(monkeypatch, db, world) -> None:
    created = _create(monkeypatch, db, world)
    client = _client_as(monkeypatch, db, world["buyer"])
    _callback(client, created["id"], 0)
    _callback(client, created["id"], 1032)

    transaction = db.get(Transaction, uuid.UUID(created["id"]))
    assert transaction is not None and transaction.status == TransactionStatus.LOCKED
    assert client.get(f"/transactions/{created['id']}").json()["payment"]["status"] == "succeeded"
