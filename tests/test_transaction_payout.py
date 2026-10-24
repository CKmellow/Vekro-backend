import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.main import app
from app.models.collection_attempt import AttemptOutcome
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus
from app.services import transaction_payout
from app.services.transaction_payout import SellerPayoutStatus, seller_payout
from sqlalchemy.orm import Session

from tests.test_transaction_read import _client_as
from tests.test_transaction_read import db as _db_fixture  # noqa: F401  (shared SQLite fixture)
from tests.test_transaction_read import parties as _parties_fixture  # noqa: F401

db = _db_fixture
parties = _parties_fixture

PAID_AT = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
NO_DB = cast(Session, None)


def _tx(status: TransactionStatus, payout=TransactionPayoutStatus.NOT_REQUIRED) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        amount=Decimal("52000.00"),
        status=status,
        payout_status=payout,
    )


def _attempt(outcome: AttemptOutcome, amount="52000.00", reference=None) -> Any:
    return SimpleNamespace(
        outcome=outcome,
        amount=Decimal(amount),
        provider_reference=reference,
        updated_at=PAID_AT,
        purpose="tx-release:x",
    )


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def attempts(monkeypatch) -> list[Any]:
    found: list[Any] = []
    monkeypatch.setattr(transaction_payout, "_release_attempts", lambda _db, _tx: found)
    return found


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TransactionStatus.AWAITING_PAYMENT, SellerPayoutStatus.NOT_PAID),
        (TransactionStatus.INITIATED, SellerPayoutStatus.NOT_PAID),
        (TransactionStatus.LOCKED, SellerPayoutStatus.HELD),
        (TransactionStatus.HOLD_24H, SellerPayoutStatus.HELD),
        (TransactionStatus.ESCALATED_ADMIN_REVIEW, SellerPayoutStatus.HELD),
        (TransactionStatus.REFUNDED_BUYER, SellerPayoutStatus.REFUNDED),
        (TransactionStatus.RESOLVED_REFUND, SellerPayoutStatus.REFUNDED),
        (TransactionStatus.RETURNED_TO_SELLER, SellerPayoutStatus.REFUNDED),
    ],
)
def test_before_release_and_after_refund(attempts, status, expected) -> None:
    payout = seller_payout(NO_DB, _tx(status))

    assert payout.status == expected
    assert (payout.amount, payout.reference, payout.paid_at) == (None, None, None)


def test_paid_out_carries_the_reference_and_time(attempts) -> None:
    attempts.append(_attempt(AttemptOutcome.SUCCEEDED, reference="QJK7ZX12AB"))

    payout = seller_payout(
        NO_DB, _tx(TransactionStatus.RELEASED, TransactionPayoutStatus.SUCCEEDED)
    )

    assert payout.status == SellerPayoutStatus.PAID_OUT
    assert payout.reference == "QJK7ZX12AB"
    assert payout.paid_at == PAID_AT
    assert payout.amount == Decimal("52000.00")


def test_queued_release_is_in_progress(attempts) -> None:
    attempts.append(_attempt(AttemptOutcome.UNKNOWN))

    payout = seller_payout(
        NO_DB, _tx(TransactionStatus.RESOLVED_RELEASE, TransactionPayoutStatus.PENDING)
    )

    assert payout.status == SellerPayoutStatus.IN_PROGRESS
    assert payout.reference is None


def test_refused_release_is_delayed(attempts) -> None:
    attempts.append(_attempt(AttemptOutcome.FAILED_DEFINITE))

    payout = seller_payout(
        NO_DB, _tx(TransactionStatus.RELEASED, TransactionPayoutStatus.FAILED_DEFINITE)
    )

    assert payout.status == SellerPayoutStatus.DELAYED


def test_split_reports_the_seller_share(attempts) -> None:
    attempts.append(_attempt(AttemptOutcome.SUCCEEDED, amount="31200.00", reference="SPLIT1"))

    payout = seller_payout(NO_DB, _tx(TransactionStatus.RESOLVED_SPLIT))

    assert payout.status == SellerPayoutStatus.PAID_OUT
    assert payout.amount == Decimal("31200.00")


def test_release_without_attempts_yet_is_in_progress(attempts) -> None:
    payout = seller_payout(NO_DB, _tx(TransactionStatus.RELEASED))

    assert payout.status == SellerPayoutStatus.IN_PROGRESS
    assert payout.amount == Decimal("52000.00")


@pytest.mark.parametrize(("who", "shown"), [("seller", True), ("admin", True), ("buyer", False)])
def test_only_the_seller_and_admins_see_the_payout(monkeypatch, db, parties, who, shown) -> None:
    client = _client_as(monkeypatch, db, parties[who])

    body = client.get(f"/transactions/{parties['transaction'].id}").json()

    if shown:
        assert body["payout"] == {
            "status": "held",
            "amount": None,
            "reference": None,
            "paid_at": None,
        }
    else:
        assert body["payout"] is None
