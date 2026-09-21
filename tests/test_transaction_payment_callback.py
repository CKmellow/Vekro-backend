import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from app.main import app
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.routers import transactions as transactions_router
from app.schemas.transaction import PaymentCallbackRequest
from app.services.transaction import (
    PaymentCallbackResult,
    TransactionNotFoundForCallbackError,
)
from app.services.transaction import (
    confirm_payment_callback as confirm_payment_callback_service,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(self, transaction: Transaction | None) -> None:
        self.transaction = transaction
        self.added: list[Any] = []
        self.committed = False
        self.refreshed = False

    def get(self, _model: Any, transaction_id):
        if self.transaction is not None and transaction_id == self.transaction.id:
            return self.transaction
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        self.refreshed = self.transaction is obj


def _build_transaction(
    status: TransactionStatus = TransactionStatus.AWAITING_PAYMENT,
    *,
    locked_at: datetime | None = None,
) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount=Decimal("5400.00"),
        status=status,
        locked_at=locked_at,
        created_at=now,
        updated_at=now,
    )


def _payload(transaction_id: uuid.UUID, result_code: int = 0) -> dict[str, Any]:
    return {
        "transaction_id": str(transaction_id),
        "result_code": result_code,
        "result_desc": "The service request is processed successfully.",
        "checkout_request_id": "ws_CO_123456",
        "merchant_request_id": "29115-34620561-1",
        "provider_reference": "mpesa-stub-ref",
    }


def test_payment_callback_endpoint_returns_200_when_transitioned(monkeypatch) -> None:
    transaction = _build_transaction(status=TransactionStatus.LOCKED, locked_at=datetime.now(UTC))

    def fake_confirm_payment_callback(_db, payload):
        assert payload.transaction_id == transaction.id
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=True,
            duplicate=False,
            detail="Transaction moved to locked.",
        )

    monkeypatch.setattr(
        transactions_router,
        "confirm_payment_callback",
        fake_confirm_payment_callback,
    )

    client = TestClient(app)
    response = client.post(
        "/transactions/payment-callback",
        json=_payload(transaction.id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transaction_id"] == str(transaction.id)
    assert body["status"] == "locked"
    assert body["transitioned"] is True
    assert body["duplicate"] is False


def test_payment_callback_endpoint_returns_200_for_duplicate(monkeypatch) -> None:
    transaction = _build_transaction(status=TransactionStatus.LOCKED, locked_at=datetime.now(UTC))

    def fake_confirm_payment_callback(_db, payload):
        assert payload.transaction_id == transaction.id
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=False,
            duplicate=True,
            detail="Duplicate callback ignored; transaction already locked.",
        )

    monkeypatch.setattr(
        transactions_router,
        "confirm_payment_callback",
        fake_confirm_payment_callback,
    )

    client = TestClient(app)
    response = client.post(
        "/transactions/payment-callback",
        json=_payload(transaction.id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "locked"
    assert body["transitioned"] is False
    assert body["duplicate"] is True


def test_payment_callback_endpoint_returns_202_for_unsuccessful_callback(monkeypatch) -> None:
    transaction = _build_transaction(status=TransactionStatus.AWAITING_PAYMENT)

    def fake_confirm_payment_callback(_db, payload):
        assert payload.transaction_id == transaction.id
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=False,
            duplicate=False,
            detail="Payment callback not successful; transition skipped.",
        )

    monkeypatch.setattr(
        transactions_router,
        "confirm_payment_callback",
        fake_confirm_payment_callback,
    )

    client = TestClient(app)
    response = client.post(
        "/transactions/payment-callback",
        json=_payload(transaction.id, result_code=1032),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "awaiting_payment"
    assert body["transitioned"] is False
    assert body["duplicate"] is False


def test_payment_callback_endpoint_returns_404_for_missing_transaction(monkeypatch) -> None:
    def fake_confirm_payment_callback(_db, payload):
        raise TransactionNotFoundForCallbackError("Transaction not found.")

    monkeypatch.setattr(
        transactions_router,
        "confirm_payment_callback",
        fake_confirm_payment_callback,
    )

    client = TestClient(app)
    response = client.post(
        "/transactions/payment-callback",
        json=_payload(uuid.uuid4()),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found."


def test_service_confirm_payment_callback_moves_to_locked_and_logs_notifications() -> None:
    transaction = _build_transaction(status=TransactionStatus.AWAITING_PAYMENT)
    db = _FakeDb(transaction=transaction)

    result = confirm_payment_callback_service(
        cast(Session, db),
        payload=PaymentCallbackRequest(**_payload(transaction.id)),
    )

    assert result.transitioned is True
    assert result.duplicate is False
    assert transaction.status == TransactionStatus.LOCKED
    assert transaction.locked_at is not None
    assert db.committed is True
    assert db.refreshed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert {item.user_id for item in notifications} == {transaction.buyer_id, transaction.seller_id}
    assert all(
        item.event_type == NotificationEventType.TRANSACTION_LOCKED for item in notifications
    )


def test_service_confirm_payment_callback_is_idempotent_for_duplicate_locked_state() -> None:
    transaction = _build_transaction(status=TransactionStatus.LOCKED, locked_at=datetime.now(UTC))
    db = _FakeDb(transaction=transaction)

    result = confirm_payment_callback_service(
        cast(Session, db),
        payload=PaymentCallbackRequest(**_payload(transaction.id)),
    )

    assert result.transitioned is False
    assert result.duplicate is True
    assert db.committed is False
    assert db.refreshed is False
    assert db.added == []


def test_service_confirm_payment_callback_skips_unsuccessful_result_code() -> None:
    transaction = _build_transaction(status=TransactionStatus.AWAITING_PAYMENT)
    db = _FakeDb(transaction=transaction)

    result = confirm_payment_callback_service(
        cast(Session, db),
        payload=PaymentCallbackRequest(**_payload(transaction.id, result_code=1)),
    )

    assert result.transitioned is False
    assert result.duplicate is False
    assert transaction.status == TransactionStatus.AWAITING_PAYMENT
    assert transaction.locked_at is None
    assert db.committed is False
    assert db.refreshed is False
    assert db.added == []


def test_service_confirm_payment_callback_skips_when_transaction_not_awaiting_payment() -> None:
    transaction = _build_transaction(status=TransactionStatus.DISPATCHED)
    db = _FakeDb(transaction=transaction)

    result = confirm_payment_callback_service(
        cast(Session, db),
        payload=PaymentCallbackRequest(**_payload(transaction.id)),
    )

    assert result.transitioned is False
    assert result.duplicate is False
    assert transaction.status == TransactionStatus.DISPATCHED
    assert db.committed is False
    assert db.refreshed is False
    assert db.added == []


def test_service_confirm_payment_callback_raises_for_missing_transaction() -> None:
    db = _FakeDb(transaction=None)

    with pytest.raises(TransactionNotFoundForCallbackError, match="Transaction not found."):
        confirm_payment_callback_service(
            cast(Session, db),
            payload=PaymentCallbackRequest(**_payload(uuid.uuid4())),
        )
