"""Issue #224: the real Daraja transport, and STK-query confirmation before an M-Pesa callback can
lock an order or record funding, in production too."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from app.core.settings import get_settings
from app.models.ledger_entry import LedgerEntry
from app.models.money_audit_event import MoneyAuditEvent
from app.models.payment_prompt import PaymentPrompt
from app.models.transaction import Transaction, TransactionStatus
from app.services import payment as payment_module
from app.services.daraja import (
    DarajaClient,
    DarajaError,
    DarajaStkGateway,
    DarajaUnavailableError,
    whole_shillings,
)
from app.services.daraja_confirmation import (
    confirm_pending_payments,
    handle_callback,
    parse_callback,
)
from app.services.payment import MpesaStkStubGateway, PaymentRequest
from app.services.payment_prompts import record_payment_prompt
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_anchoring_batcher import _db, _settings
from tests.test_simulated_custody_full_flow import _seed_transaction_context

NOW = datetime(2026, 11, 8, 6, 30, 5, tzinfo=UTC)  # 09:30:05 in Nairobi
CHECKOUT = "ws_CO_08112026093005123456789"


class FakeDaraja:
    """Daraja's three endpoints, answering with the shapes Safaricom documents."""

    def __init__(self) -> None:
        self.query: Callable[[], tuple[int, dict[str, Any]]] = lambda: (
            200,
            {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."},
        )
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth/v1/generate":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": "3599"})
        assert request.headers["Authorization"] == "Bearer tok-1"
        if request.url.path == "/mpesa/stkpush/v1/processrequest":
            return httpx.Response(
                200,
                json={
                    "MerchantRequestID": "29115-34620561-1",
                    "CheckoutRequestID": CHECKOUT,
                    "ResponseCode": "0",
                    "ResponseDescription": "Success. Request accepted for processing",
                    "CustomerMessage": "Success. Request accepted for processing",
                },
            )
        if request.url.path == "/mpesa/stkpushquery/v1/query":
            status, body = self.query()
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    def client(self) -> DarajaClient:
        return DarajaClient(
            environment="sandbox",
            consumer_key="key",
            consumer_secret="secret",
            shortcode="174379",
            passkey="passkey",
            callback_url="https://api.vekro.co.ke/payments/mpesa/callback",
            transport=httpx.MockTransport(self.handler),
            now=lambda: NOW,
        )


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def test_stk_push_sends_the_documented_request_and_reuses_the_token() -> None:
    fake = FakeDaraja()
    gateway = DarajaStkGateway(fake.client())
    response = gateway.initiate_stk_push(
        PaymentRequest(
            transaction_id="t1",
            amount="1500.00",
            phone_number="0712345678",
            account_reference="VK-7K3P9Q",
            transaction_desc="Escrow payment",
        )
    )
    assert response.checkout_request_id == CHECKOUT
    push = _body(fake.requests[1])
    assert push["Amount"] == 1500 and push["PartyA"] == push["PhoneNumber"] == "254712345678"
    assert push["Timestamp"] == "20261108093005"  # Nairobi time
    assert base64.b64decode(push["Password"]).decode() == "174379passkey20261108093005"
    assert push["CallBackURL"].endswith("/payments/mpesa/callback")
    assert (
        fake.requests[0].headers["Authorization"]
        == "Basic " + base64.b64encode(b"key:secret").decode()
    )

    gateway.initiate_stk_push(
        PaymentRequest("t2", "100.00", "0712345678", "VK-2", "Escrow payment")
    )
    assert fake.token_calls == 1


def test_stk_query_states() -> None:
    fake = FakeDaraja()
    client = fake.client()
    assert client.stk_query(CHECKOUT).state == "succeeded"

    fake.query = lambda: (200, {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"})
    cancelled = client.stk_query(CHECKOUT)
    assert (cancelled.state, cancelled.result_code) == ("failed", 1032)

    fake.query = lambda: (
        500,
        {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"},
    )
    assert client.stk_query(CHECKOUT).state == "pending"

    fake.query = lambda: (503, {"errorCode": "503.001.01"})
    with pytest.raises(DarajaUnavailableError):
        client.stk_query(CHECKOUT)


def test_refusals_and_bad_amounts() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": "3599"})
        return httpx.Response(400, json={"errorCode": "400.002.02", "errorMessage": "Bad Request"})

    client = DarajaClient(
        environment="sandbox",
        consumer_key="k",
        consumer_secret="s",
        shortcode="174379",
        passkey="p",
        callback_url="https://x",
        transport=httpx.MockTransport(refuse),
    )
    with pytest.raises(DarajaError, match="refused"):
        client.stk_push(phone="254712345678", amount=10, account_reference="r", description="d")
    with pytest.raises(ValueError):
        whole_shillings("10.50")
    assert whole_shillings("1500.00") == 1500


def test_gateway_selection_follows_daraja_enabled(monkeypatch) -> None:
    assert isinstance(payment_module.get_payment_gateway(), MpesaStkStubGateway)
    enabled = get_settings().model_copy(
        update={
            "daraja_enabled": True,
            "daraja_consumer_key": "k",
            "daraja_consumer_secret": "s",
            "daraja_passkey": "p",
            "daraja_callback_url": "https://x",
        }
    )
    import app.core.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: enabled)
    assert isinstance(payment_module.get_payment_gateway(), DarajaStkGateway)


def test_enabling_daraja_requires_its_credentials_without_echoing_them() -> None:
    with pytest.raises(ValueError) as error:
        _settings(daraja_enabled=True, daraja_consumer_key="", daraja_passkey="hunter2-passkey")
    assert "DARAJA_CONSUMER_KEY" in str(error.value)
    assert "hunter2-passkey" not in str(error.value)


def _awaiting(db: Session, suffix: str) -> tuple[Transaction, PaymentPrompt]:
    _buyer, _seller, _listing, transaction = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )
    response = payment_module.PaymentResponse(
        provider="mpesa_daraja",
        status="queued",
        provider_request_id="m1",
        merchant_request_id="m1",
        checkout_request_id=f"{CHECKOUT}-{suffix}",
        customer_message="ok",
    )
    prompt = record_payment_prompt(db, transaction, payment_response=response)
    return transaction, prompt


def _callback(checkout: str, *, amount: Any, result_code: int = 0) -> dict[str, Any]:
    return {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "29115-34620561-1",
                "CheckoutRequestID": checkout,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": amount},
                        {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
                        {"Name": "TransactionDate", "Value": 20261108093005},
                        {"Name": "PhoneNumber", "Value": 254712345678},
                    ]
                },
            }
        }
    }


def _funding(db: Session) -> list[LedgerEntry]:
    return list(
        db.execute(select(LedgerEntry).where(LedgerEntry.account_code == "ESCROW_HELD")).scalars()
    )


@pytest.fixture
def production(monkeypatch):
    """Funding from a callback used to be impossible in production; a confirmed one now posts."""
    prod = get_settings().model_copy(update={"environment": "production"})
    from app.services import transaction as transaction_service

    monkeypatch.setattr(transaction_service, "get_settings", lambda: prod)


def test_a_confirmed_callback_locks_and_funds_even_in_production(production) -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "61")
    fake = FakeDaraja()

    body = _callback(prompt.checkout_request_id or "", amount=float(transaction.amount))
    assert handle_callback(db, body, client=fake.client()) == "confirmed"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert [entry.amount for entry in _funding(db)] == [transaction.amount]
    audit = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "callback_funding_confirmed")
    ).scalar_one()
    assert (audit.rail_name, audit.provider_reference) == ("daraja", "NLJ7RT61SV")

    # A repeat callback changes nothing and posts nothing twice.
    assert handle_callback(db, body, client=fake.client()) == "already_processed"
    assert len(_funding(db)) == 1


def test_a_callback_the_query_does_not_confirm_never_locks(production) -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "62")
    fake = FakeDaraja()
    fake.query = lambda: (200, {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"})

    body = _callback(prompt.checkout_request_id or "", amount=float(transaction.amount))
    assert handle_callback(db, body, client=fake.client()) == "failed"
    db.refresh(transaction)
    db.refresh(prompt)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert (prompt.status, prompt.failure_reason) == ("failed", "cancelled")
    assert _funding(db) == []


def test_a_pending_or_unreachable_query_leaves_the_order_waiting(production) -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "63")
    fake = FakeDaraja()
    body = _callback(prompt.checkout_request_id or "", amount=float(transaction.amount))

    fake.query = lambda: (500, {"errorCode": "500.001.1001", "errorMessage": "processing"})
    assert handle_callback(db, body, client=fake.client()) == "pending"
    fake.query = lambda: (502, {"errorCode": "502"})
    assert handle_callback(db, body, client=fake.client()) == "unconfirmed"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert _funding(db) == []

    # The reconciliation command settles it once Daraja confirms.
    fake.query = lambda: (200, {"ResultCode": "0", "ResultDesc": "ok"})
    outcomes = confirm_pending_payments(db, client=fake.client())
    assert outcomes == {transaction.id: "confirmed"}
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED


def test_a_callback_with_the_wrong_amount_is_refused_and_audited(production) -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "64")
    fake = FakeDaraja()
    body = _callback(prompt.checkout_request_id or "", amount=1)

    assert handle_callback(db, body, client=fake.client()) == "amount_mismatch"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    audit = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "daraja_callback_amount_mismatch")
    ).scalar_one()
    assert audit.amount == Decimal("1.00")
    assert not any(r.url.path.endswith("/query") for r in fake.requests)  # never queried


def test_unknown_or_unreadable_callbacks_change_nothing() -> None:
    db = _db()
    fake = FakeDaraja()
    assert handle_callback(db, {"hello": "world"}, client=fake.client()) == "unreadable"
    assert handle_callback(db, _callback("ws_CO_unknown", amount=10), client=fake.client()) == (
        "unknown_checkout"
    )
    assert fake.requests == []
    parsed = parse_callback(_callback("x", amount="2500"))
    assert parsed is not None and parsed.amount == Decimal("2500")


def test_unknown_checkout_ids_are_logged_without_the_subscriber_number(caplog) -> None:
    # Observed in the sandbox: the ID ends with the payer's 9-digit subscriber number.
    checkout = "ws_CO_081120261437512712345678"
    with caplog.at_level("WARNING", logger="app.payments.daraja"):
        handle_callback(_db(), _callback(checkout, amount="1"), client=FakeDaraja().client())
    assert "ws_CO_08112026143751…" in caplog.text
    assert "712345678" not in caplog.text


def test_the_webhook_always_answers_accepted(monkeypatch) -> None:
    from app.main import app
    from fastapi.testclient import TestClient

    response = TestClient(app).post("/payments/mpesa/callback", json={"Body": {}})
    assert response.status_code == 200
    assert response.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}


def test_observed_sandbox_shapes_parse() -> None:
    """tests/fixtures/daraja: shapes the sandbox actually sent (docs/daraja-observed.md)."""
    fixtures = Path(__file__).parent / "fixtures" / "daraja"
    cancelled = parse_callback(json.loads((fixtures / "stk_callback_cancelled.json").read_text()))
    assert cancelled is not None
    assert (cancelled.result_code, cancelled.amount, cancelled.receipt) == (1032, None, None)
    token = json.loads((fixtures / "oauth_token.json").read_text())
    assert isinstance(token["expires_in"], str)  # a string, not a number
