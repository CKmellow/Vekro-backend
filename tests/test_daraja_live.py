"""Opt-in Daraja sandbox round trip (M22). Each run sends ONE real KES 1 STK push; never by default.

    RUN_DARAJA_LIVE_TESTS=1 DARAJA_LIVE_MODE=callback|fallback .venv/bin/pytest -m live -k daraja -s

Needs the DARAJA_* credentials and DARAJA_LIVE_TEST_PHONE (the Safaricom number that gets the
prompt; never printed, masked in fixtures). The order is created through ``create_transaction``
with the real Daraja gateway, in a scratch SQLite database (DARAJA_LIVE_DB, default under pytest's
tmp dir). One run funds one order, by one of the two paths (docs/demo-modes.md):

- ``callback``: the backend runs on DARAJA_LIVE_DB behind ``make tunnel``. The test sends no STK
  query of its own (two queriers trip the sandbox's 429 limit); it waits for the callback, and the
  query it triggers, to lock the order.
- ``fallback``: the tunnel is stopped, so no callback can arrive. The real fallback,
  ``watch_pending_payments`` (``confirm-mpesa-payments --watch``), confirms the order with backoff.

Sanitised request and response shapes are written to tests/fixtures/daraja/.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from app.core.settings import get_settings
from app.db.base import Base
from app.models.listing import Listing
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import CreateTransactionRequest
from app.services.daraja import DarajaClient, DarajaStkGateway, DarajaUnavailableError
from app.services.daraja_confirmation import watch_pending_payments
from app.services.transaction import create_transaction
from dotenv import dotenv_values
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

FIXTURES = Path(__file__).parent / "fixtures" / "daraja"
MASKED_PHONE = "254700000000"
QUERY_BUDGET_SECONDS = 240
CALLBACK_WAIT_SECONDS = 180
NGROK_API = "http://127.0.0.1:4040/api/requests/http?limit=50"


def _env(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        value = dotenv_values(Path(__file__).parent.parent / ".env").get(name) or ""
    return value.strip()


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        _env("RUN_DARAJA_LIVE_TESTS").lower() not in {"1", "true", "yes"},
        reason="Opt-in live test. Set RUN_DARAJA_LIVE_TESTS=1 to send one sandbox STK push.",
    ),
]


class _Recorder(httpx.BaseTransport):
    """Passes requests through to Daraja and keeps each (path, status, JSON body)."""

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.exchanges: list[tuple[str, int, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        response.read()
        try:
            body = response.json()
        except ValueError:
            body = None
        self.exchanges.append((request.url.path, response.status_code, body))
        return response


def _sanitise(value: Any, phone_digits: str) -> Any:
    if isinstance(value, dict):
        items = {k: _sanitise(v, phone_digits) for k, v in value.items()}
        if "access_token" in items:
            items["access_token"] = "<redacted>"
        if items.get("Name") == "MpesaReceiptNumber":
            items["Value"] = "XXXXXXXXXX"
        if items.get("Name") == "PhoneNumber":
            items["Value"] = int(MASKED_PHONE)
        return items
    if isinstance(value, list):
        return [_sanitise(v, phone_digits) for v in value]
    if isinstance(value, str):
        # CheckoutRequestID ends with the payer's 9-digit subscriber number.
        return value.replace(phone_digits, MASKED_PHONE).replace(phone_digits[3:], MASKED_PHONE[3:])
    if isinstance(value, int) and str(value) == phone_digits:
        return int(MASKED_PHONE)
    return value


def _save(name: str, body: Any, phone_digits: str) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    text_body = json.dumps(_sanitise(body, phone_digits), indent=2, sort_keys=True)
    assert phone_digits[3:] not in text_body
    assert not re.search(r"2547\d{8}", text_body.replace(MASKED_PHONE, ""))
    (FIXTURES / name).write_text(text_body + "\n")


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _schema(url: str) -> None:
    """The app's tables on SQLite; Postgres-only JSONB defaults become plain '{}' (as in tests)."""
    engine = create_engine(url)
    patched = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            default = column.server_default
            if default is not None and "::jsonb" in str(getattr(default, "arg", "")):
                patched[column] = default
                column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(engine)
    finally:
        for column, default in patched.items():
            column.server_default = default
    engine.dispose()


def _seed(db: Session, phone: str) -> tuple[User, Listing]:
    now = datetime.now(UTC)

    def user(role: UserRole, user_phone: str) -> User:
        return User(
            id=uuid.uuid4(),
            name=f"Live {role.value}",
            phone=user_phone,
            role=role,
            password_hash="pbkdf2_sha256$1$abc$xyz",
            mpesa_phone=user_phone,
            session_version=1,
            is_active=True,
            failed_login_attempts=0,
            created_at=now,
            updated_at=now,
        )

    buyer = user(UserRole.BUYER, f"+{phone}")
    seller = user(UserRole.SELLER, "+254700000001")
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title="Daraja sandbox round trip",
        price=Decimal("1.00"),
        is_serialized=False,
        dispute_policy={},
        created_at=now,
        updated_at=now,
    )
    db.add_all([buyer, seller, listing])
    db.commit()
    return buyer, listing


def _ngrok_callback(checkout: str) -> Any:
    """The callback body Daraja posted, as ngrok's local inspector recorded it (if running)."""
    try:
        requests = httpx.get(NGROK_API, timeout=5).json().get("requests", [])
    except (httpx.HTTPError, ValueError):
        return None
    for entry in requests:
        raw = (entry.get("request") or {}).get("raw") or ""
        if checkout in _decoded(raw):
            body = _decoded(raw).split("\r\n\r\n", 1)[-1]
            return json.loads(body)
    return None


def _decoded(raw: str) -> str:
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except ValueError:
        return ""


def test_daraja_sandbox_stk_round_trip(tmp_path: Path) -> None:
    mode = _env("DARAJA_LIVE_MODE") or "fallback"
    if mode not in {"callback", "fallback"}:
        pytest.fail("DARAJA_LIVE_MODE must be callback or fallback.")
    phone = re.sub(r"\D", "", _env("DARAJA_LIVE_TEST_PHONE"))
    if not re.fullmatch(r"254\d{9}", phone):
        pytest.skip("Set DARAJA_LIVE_TEST_PHONE (2547XXXXXXXX) in .env.")
    settings = get_settings().model_copy(update={"daraja_enabled": True, "econfirm_enabled": False})
    if not settings.daraja_callback_url.startswith("https://"):
        pytest.skip("Set DARAJA_CALLBACK_URL to the tunnel's https URL.")

    db_path = Path(_env("DARAJA_LIVE_DB") or tmp_path / "daraja-live.sqlite3").resolve()
    db_path.unlink(missing_ok=True)
    url = f"sqlite:///{db_path}"
    _schema(url)
    engine = create_engine(url)

    recorder = _Recorder()
    client = DarajaClient.from_settings(settings, transport=recorder)
    with Session(engine, expire_on_commit=False) as db:
        buyer, listing = _seed(db, phone)
        transaction = create_transaction(
            db,
            buyer,
            CreateTransactionRequest(listing_id=listing.id),
            payment_gateway=DarajaStkGateway(client),
            settings=settings,
        )
        prompt = db.execute(
            select(PaymentPrompt).where(PaymentPrompt.transaction_id == transaction.id)
        ).scalar_one()
        checkout = prompt.checkout_request_id
        reference = transaction.reference
    assert checkout, "Daraja accepted no STK push"
    print(f"\nSTK push sent for {reference} (KES 1, {mode} path). Approve it on the phone now.")

    if mode == "callback":
        status = _wait_for_status(engine, reference, seconds=CALLBACK_WAIT_SECONDS)
        print(f"After the callback the order is {status}.")
        callback = _ngrok_callback(checkout)
        if callback is not None:
            _save("stk_callback_paid.json", callback, phone)
        assert status == TransactionStatus.LOCKED
        try:  # one query after the fact, for the fixture only; the sandbox may answer 429
            client.stk_query(checkout)
        except DarajaUnavailableError:
            pass
    else:
        with Session(engine, expire_on_commit=False) as db:
            outcomes = watch_pending_payments(
                db, client=client, duration_seconds=QUERY_BUDGET_SECONDS, stop_when_idle=True
            )
        status = _wait_for_status(engine, reference, seconds=0)
        print(f"Fallback outcomes: {list(outcomes.values())}; the order is {status}.")
        assert status == TransactionStatus.LOCKED

    by_path: dict[str, list[tuple[int, Any]]] = {}
    for path, http_status, body in recorder.exchanges:
        by_path.setdefault(path, []).append((http_status, body))
    _save("oauth_token.json", by_path["/oauth/v1/generate"][0][1], phone)
    _save("stk_push_accepted.json", by_path["/mpesa/stkpush/v1/processrequest"][0][1], phone)
    for http_status, body in by_path.get("/mpesa/stkpushquery/v1/query", []):
        if http_status == 429:
            name = "stk_query_rate_limited.json"
        elif http_status != 200:
            name = "stk_query_pending.json"
        else:
            name = "stk_query_paid.json"
        _save(name, {"http_status": http_status, "body": body}, phone)
    engine.dispose()


def _wait_for_status(engine: Any, reference: str, *, seconds: float) -> TransactionStatus:
    deadline = time.monotonic() + seconds
    while True:
        with Session(engine) as db:
            status = db.execute(
                select(Transaction.status).where(Transaction.reference == reference)
            ).scalar_one()
        if status != TransactionStatus.AWAITING_PAYMENT or time.monotonic() >= deadline:
            return status
        time.sleep(3)
