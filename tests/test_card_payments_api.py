"""#251 at the API: card orders, the refresh endpoint and the IPN's documented answer.

Uses the payment prompt fixtures (no ledger tables needed); funding is covered in
tests/test_card_payments.py.
"""

# ruff: noqa: F811  (the db and world fixtures are imported, then requested by name)
from typing import Any

from tests.test_card_payments import FakePesapal, card_settings
from tests.test_payment_prompts import _client_as, db, world  # noqa: F401


def _card_api(monkeypatch, fake: FakePesapal, **settings: Any) -> None:
    configured = card_settings(**settings)
    for target in (
        "app.services.transaction",
        "app.routers.transactions",
        "app.routers.pesapal_card",
    ):
        monkeypatch.setattr(f"{target}.get_settings", lambda: configured)
    monkeypatch.setattr("app.services.transaction.build_card_rail", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.routers.transactions.build_card_rail", lambda *_a, **_k: fake)
    monkeypatch.setattr("app.routers.pesapal_card.build_rail", lambda *_a, **_k: fake)


def test_api_card_order_returns_a_redirect_instruction(monkeypatch, db, world) -> None:
    _card_api(monkeypatch, FakePesapal())
    client = _client_as(monkeypatch, db, world["buyer"])

    response = client.post(
        "/transactions", json={"listing_id": str(world["listing"].id), "payment_method": "card"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["payment_method"] == "card"
    assert body["payment"]["kind"] == "REDIRECT"
    assert body["payment"]["redirect_url"].startswith("https://cybqa.pesapal.com/")


def test_api_card_is_422_when_not_configured(monkeypatch, db, world) -> None:
    _card_api(monkeypatch, FakePesapal(), pesapal_enabled=False)
    client = _client_as(monkeypatch, db, world["buyer"])

    response = client.post(
        "/transactions", json={"listing_id": str(world["listing"].id), "payment_method": "card"}
    )

    assert response.status_code == 422
    assert "Card payments are not available" in response.json()["detail"]


def test_api_refresh_is_only_for_card_orders(monkeypatch, db, world) -> None:
    _card_api(monkeypatch, FakePesapal())
    client = _client_as(monkeypatch, db, world["buyer"])
    created = client.post("/transactions", json={"listing_id": str(world["listing"].id)}).json()

    assert client.post(f"/transactions/{created['id']}/payment/refresh").status_code == 422


def test_api_ipn_answers_pesapals_documented_shape(monkeypatch, db, world) -> None:
    from app.db.session import get_db
    from app.main import app
    from fastapi.testclient import TestClient

    _card_api(monkeypatch, FakePesapal())
    app.dependency_overrides[get_db] = lambda: db
    query = (
        "OrderTrackingId=nobody&OrderMerchantReference=VK-NOPE00&OrderNotificationType=IPNCHANGE"
    )

    got = TestClient(app).get(f"/payments/pesapal/ipn?{query}")
    posted = TestClient(app).post(
        "/payments/pesapal/ipn",
        json={"OrderTrackingId": "nobody", "OrderMerchantReference": "VK-NOPE00"},
    )

    assert got.status_code == posted.status_code == 200
    assert got.json() == {
        "orderNotificationType": "IPNCHANGE",
        "orderTrackingId": "nobody",
        "orderMerchantReference": "VK-NOPE00",
        "status": 500,
    }
    assert posted.json()["status"] == 500


def test_api_manual_refunds_are_admin_only(monkeypatch, db, world) -> None:
    client = _client_as(monkeypatch, db, world["buyer"])

    assert client.get("/admin/refunds/manual").status_code == 403
    assert (
        client.post(
            "/admin/refunds/00000000-0000-4000-8000-000000000000/complete", json={"note": "done"}
        ).status_code
        == 403
    )
