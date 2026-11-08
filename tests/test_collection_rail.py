"""#257: every order records the rail that collects its money and whether that is sandbox or live,
and the funding event anchors both."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.models.anchor_event import AnchorEvent
from app.models.money_audit_event import MoneyAuditEvent
from app.schemas.transaction import CreateTransactionRequest, TransactionResponse
from app.services.daraja_confirmation import handle_callback
from app.services.payment_rails import rail_mode
from app.services.transaction import create_transaction
from sqlalchemy import select

from tests.test_anchoring_batcher import _db
from tests.test_card_payments import FakePesapal, _card_order, _ipn, card_settings, handle_ipn
from tests.test_daraja import FakeDaraja, _awaiting, _callback
from tests.test_simulated_custody_full_flow import _seed_transaction_context


@pytest.mark.parametrize(
    ("rail", "settings", "mode"),
    [
        ("daraja", {"daraja_env": "sandbox"}, "sandbox"),
        ("daraja", {"daraja_env": "production"}, "live"),
        ("pesapal", {"pesapal_base_url": "https://cybqa.pesapal.com/pesapalv3"}, "sandbox"),
        ("pesapal", {"pesapal_base_url": "https://pay.pesapal.com/v3"}, "live"),
        ("econfirm", {"econfirm_base_url": "https://api.econfirm.co.ke"}, "live"),
        ("econfirm", {"econfirm_base_url": "https://staging.econfirm.co.ke"}, "sandbox"),
        ("simulated", {}, "sandbox"),
    ],
)
def test_sandbox_or_live_follows_the_rails_configuration(rail, settings, mode) -> None:
    assert rail_mode(SimpleNamespace(**settings), rail) == mode


def _mpesa_order(db, suffix: str, **settings):
    buyer, _seller, listing, _ = _seed_transaction_context(
        db, serialized_listing=False, suffix=suffix
    )

    class _Gateway:
        def initiate_stk_push(self, request):
            from app.services.payment import PaymentResponse

            return PaymentResponse("fake", "queued", "r", "m", f"ws_CO_{suffix}", "ok")

    return create_transaction(
        db,
        buyer,
        CreateTransactionRequest(listing_id=listing.id),
        payment_gateway=_Gateway(),
        settings=card_settings(**settings),
    )


def test_an_mpesa_order_is_simulated_unless_daraja_is_on() -> None:
    db = _db()
    simulated = _mpesa_order(db, "81", daraja_enabled=False)
    daraja = _mpesa_order(db, "82", daraja_enabled=True, daraja_env="sandbox")
    live = _mpesa_order(db, "83", daraja_enabled=True, daraja_env="production")

    assert (simulated.collection_rail, simulated.payment_mode) == ("simulated", "sandbox")
    assert (daraja.collection_rail, daraja.payment_mode) == ("daraja", "sandbox")
    assert (live.collection_rail, live.payment_mode) == ("daraja", "live")


def test_a_card_order_is_pesapal_and_its_funding_anchors_rail_and_mode() -> None:
    db = _db()
    fake = FakePesapal()
    transaction, prompt = _card_order(db, "84", fake)
    assert (transaction.collection_rail, transaction.payment_mode) == ("pesapal", "sandbox")

    handle_ipn(db, _ipn(transaction, prompt), rail=fake)

    funded = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "callback_funding_confirmed")
    ).scalar_one()
    assert funded.details == {"collection_rail": "pesapal", "payment_mode": "sandbox"}
    payloads = [
        json.loads(e.payload)
        for e in db.execute(
            select(AnchorEvent).where(AnchorEvent.transaction_id == transaction.id)
        ).scalars()
    ]
    (anchored,) = [p for p in payloads if p.get("action") == "callback_funding_confirmed"]
    assert anchored["details"] == {"collection_rail": "pesapal", "payment_mode": "sandbox"}


def test_a_daraja_funding_anchors_rail_and_mode() -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "85")
    transaction.collection_rail, transaction.payment_mode = "daraja", "sandbox"
    db.commit()

    body = _callback(prompt.checkout_request_id or "", amount=float(transaction.amount))
    assert handle_callback(db, body, client=FakeDaraja().client()) == "confirmed"

    funded = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "callback_funding_confirmed")
    ).scalar_one()
    assert funded.details == {"collection_rail": "daraja", "payment_mode": "sandbox"}


def test_the_api_reports_rail_and_mode_and_never_invents_live() -> None:
    db = _db()
    transaction = _mpesa_order(db, "86", daraja_enabled=True, daraja_env="sandbox")
    body = TransactionResponse.model_validate(transaction).model_dump()
    assert (body["collection_rail"], body["payment_mode"]) == ("daraja", "sandbox")

    transaction.collection_rail, transaction.payment_mode = "unknown", None
    body = TransactionResponse.model_validate(transaction).model_dump()
    assert (body["collection_rail"], body["payment_mode"]) == ("simulated", "sandbox")
