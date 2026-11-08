"""#241: raw provider references never reach anything both parties can read.

Daraja's CheckoutRequestID ends with the payer's subscriber number (docs/daraja-observed.md), and
every order payload is served to the seller as well as the buyer. Only the keyed provider_ref may
appear in money_audit_events, anchored payloads, notifications and the verification bundle.
"""

import json

from app.core.settings import get_settings
from app.models.anchor_event import AnchorEvent
from app.models.money_audit_event import MoneyAuditEvent
from app.models.notification import Notification
from app.services.anchoring.provider_ref import provider_ref
from app.services.anchoring.verification import build_transaction_verification
from app.services.audit import MoneyAuditQuery, list_money_audit_events
from app.services.daraja_confirmation import handle_callback
from sqlalchemy import select

from tests.pii_guard import pii_findings
from tests.test_daraja import FakeDaraja, _awaiting, _callback, _db

# Shaped like the sandbox's: timestamp, one digit, then a 9-digit subscriber number.
SUBSCRIBER = "712345678"
RECEIPT = "NLJ7RT61SV"


def test_the_guard_flags_personal_data_and_spares_identifiers() -> None:
    assert pii_findings({"provider_reference": f"ws_CO_081120261437512{SUBSCRIBER}"})
    assert pii_findings({"phone": "+254712345678"})
    assert pii_findings({"phone": "0712345678"})
    assert pii_findings({"PhoneNumber": 254712345678})
    assert pii_findings({"email": "buyer@example.com"})
    assert not pii_findings(
        {
            "transaction_id": "5b0e6c1e-1f6a-4b8e-9c2d-3f4a5b6c7d8e",
            "provider_ref": provider_ref("ws_CO_x"),
            "correlation_id": f"txn=-|ref={provider_ref('ws_CO_x')}|rail=daraja",
            "amount": "250000.00",
            "at": "2026-11-08T11:37:51.522768Z",
        }
    )


def test_daraja_funding_leaves_no_raw_reference_where_the_seller_can_read() -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "71")
    checkout = f"ws_CO_081120261437512{SUBSCRIBER}"
    prompt.checkout_request_id = checkout
    db.commit()

    body = _callback(checkout, amount=float(transaction.amount))
    assert handle_callback(db, body, client=FakeDaraja().client()) == "confirmed"

    secrets = (checkout, SUBSCRIBER, RECEIPT)
    payloads = [e.payload for e in db.execute(select(AnchorEvent)).scalars()]
    assert payloads
    for payload in payloads:
        assert not any(secret in payload for secret in secrets)
        assert pii_findings(payload) == []

    for row in db.execute(select(MoneyAuditEvent)).scalars():
        stored = f"{row.provider_reference}|{row.correlation_id}|{json.dumps(row.details)}"
        assert not any(secret in stored for secret in secrets)

    seller_notes = db.execute(
        select(Notification).where(Notification.user_id == transaction.seller_id)
    ).scalars()
    for note in seller_notes:
        assert not any(secret in json.dumps(note.payload) for secret in secrets)

    bundle = json.dumps(
        build_transaction_verification(db, transaction, get_settings()), default=str
    )
    assert not any(secret in bundle for secret in secrets)


def test_admins_still_find_audit_events_by_the_providers_reference() -> None:
    db = _db()
    transaction, prompt = _awaiting(db, "72")
    body = _callback(prompt.checkout_request_id or "", amount=float(transaction.amount))
    handle_callback(db, body, client=FakeDaraja().client())

    by_raw = list_money_audit_events(db, MoneyAuditQuery(provider_reference=RECEIPT))
    by_ref = list_money_audit_events(db, MoneyAuditQuery(provider_reference=provider_ref(RECEIPT)))

    assert [e.action for e in by_raw] == ["callback_funding_confirmed"]
    assert [e.id for e in by_ref] == [e.id for e in by_raw]
