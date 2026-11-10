"""#308: the card return page's status read works without a session.

Keyed by the order reference and the Pesapal tracking id from the return URL; status only.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.db.session import get_db
from app.main import app
from app.models.transaction import TransactionStatus
from app.services import public_order
from app.services.custody.enums import CollectionOutcome
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from tests.test_anchoring_batcher import _db
from tests.test_card_payments import FakePesapal, _audits, _card_order, _funding, card_settings


def _threadsafe_engine(url: str, **kwargs):
    """The API runs sync routes in a worker thread: share the one in-memory SQLite connection."""
    return create_engine(
        url, connect_args={"check_same_thread": False}, poolclass=StaticPool, **kwargs
    )


@pytest.fixture
def setup(monkeypatch) -> Iterator[tuple]:
    public_order._BUCKETS.clear()
    monkeypatch.setattr("tests.test_simulated_custody_full_flow.create_engine", _threadsafe_engine)
    db, fake = _db(), FakePesapal()
    transaction, prompt = _card_order(db, "90", fake)
    monkeypatch.setattr("app.routers.public_orders.get_settings", lambda: card_settings())
    monkeypatch.setattr("app.routers.public_orders.build_rail", lambda *_a, **_k: fake)
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app), db, fake, transaction, prompt
    finally:
        app.dependency_overrides.pop(get_db, None)
        public_order._BUCKETS.clear()


def _url(transaction, tracking_id: str) -> str:
    return f"/public/orders/{transaction.reference}/card-payment?tracking_id={tracking_id}"


def test_get_reads_the_status_without_a_session_and_without_asking_pesapal(setup) -> None:
    client, _db, fake, transaction, prompt = setup

    response = client.get(_url(transaction, prompt.checkout_request_id))

    assert response.status_code == 200
    assert response.json() == {
        "reference": transaction.reference,
        "state_group": "unpaid",
        "payment_status": "pending",
        "failure_reason": None,
    }
    assert fake.queries == []


def test_post_checks_with_pesapal_and_funds_once(setup) -> None:
    client, db, fake, transaction, prompt = setup

    first = client.post(_url(transaction, prompt.checkout_request_id))
    again = client.post(_url(transaction, prompt.checkout_request_id))

    assert first.status_code == again.status_code == 200
    assert first.json()["state_group"] == "paid"
    assert first.json()["payment_status"] == "succeeded"
    db.refresh(transaction)
    assert transaction.status == TransactionStatus.LOCKED
    assert len(_funding(db, transaction)) == 1
    assert len(_audits(db, "callback_funding_confirmed")) == 1
    assert fake.queries == [prompt.checkout_request_id]  # the second check did not ask


def test_post_after_a_decline_and_a_retry_on_the_same_order_shows_paid(setup) -> None:
    client, db, fake, transaction, prompt = setup
    paid = fake.status
    fake.status = FakePesapal.with_outcome(CollectionOutcome.FAILED_DEFINITE, "failed")

    declined = client.post(_url(transaction, prompt.checkout_request_id)).json()
    fake.status = paid
    retried = client.post(_url(transaction, prompt.checkout_request_id)).json()

    assert (declined["payment_status"], declined["failure_reason"]) == ("failed", "declined")
    assert (retried["state_group"], retried["payment_status"]) == ("paid", "succeeded")
    assert len(_funding(db, transaction)) == 1


def test_the_answer_carries_no_personal_data_ids_or_amounts(setup) -> None:
    client, _db, _fake, transaction, prompt = setup

    body = client.post(_url(transaction, prompt.checkout_request_id)).json()

    assert set(body) == {"reference", "state_group", "payment_status", "failure_reason"}
    text = str(body)
    assert str(transaction.id) not in text and prompt.checkout_request_id not in text
    assert str(transaction.amount) not in text


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_wrong_tracking_id_or_reference_is_the_same_404(setup, method) -> None:
    client, _db, fake, transaction, prompt = setup
    call = getattr(client, method)

    wrong_tracking = call(_url(transaction, "not-a-tracking-id"))
    wrong_reference = call(
        f"/public/orders/VK-OTHER0/card-payment?tracking_id={prompt.checkout_request_id}"
    )
    missing = call(f"/public/orders/{transaction.reference}/card-payment")

    assert wrong_tracking.status_code == wrong_reference.status_code == 404
    assert wrong_tracking.json() == wrong_reference.json()
    assert missing.status_code == 422
    assert fake.queries == []


def test_the_reference_is_case_insensitive(setup) -> None:
    client, _db, _fake, transaction, prompt = setup
    url = f"/public/orders/{transaction.reference.lower()}/card-payment"

    assert client.get(f"{url}?tracking_id={prompt.checkout_request_id}").status_code == 200


def test_lookups_are_rate_limited(setup) -> None:
    client, _db, _fake, transaction, prompt = setup
    url = _url(transaction, prompt.checkout_request_id)

    codes = [client.get(url).status_code for _ in range(public_order.LOOKUPS_PER_MINUTE + 1)]

    assert codes[-1] == 429 and set(codes[:-1]) == {200}
