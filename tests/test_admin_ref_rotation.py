"""#295: rotating ANCHOR_ADMIN_REF_SECRET (new key first, old key kept, docs/hosting.md section 13)
leaves every record made under the old key verifiable by the full audit, and the late M-Pesa
check still finds an audit row written before the rotation (no second alert)."""

from __future__ import annotations

from decimal import Decimal

from app.core.settings import get_settings
from app.models.dispute import AdminDecision, Dispute
from app.services.anchoring.full_audit import run_full_audit
from app.services.anchoring.provider_ref import candidate_refs, provider_ref
from app.services.daraja_confirmation import handle_callback
from app.services.dispute import force_resolve_dispute_case
from app.services.payment_switch import LATE_MPESA, record_mpesa_after_card_switch
from sqlalchemy import select

from tests.anchoring_fakes import FakeRegistry
from tests.test_anchor_events import _admin, _funded_dispute_case
from tests.test_anchoring_batcher import _db
from tests.test_anchoring_track_record import _anchor_everything, _released
from tests.test_card_payments import _audits
from tests.test_daraja import _callback
from tests.test_payment_switch import PAID, _daraja, _switched_order

OLD = "o" * 32
NEW = "n" * 32


def _use_keys(monkeypatch, *keys: str) -> None:
    settings = get_settings().model_copy(update={"anchor_admin_ref_secret": ",".join(keys)})
    monkeypatch.setattr("app.core.settings.get_settings", lambda: settings)


def _anchored_under_the_old_key(monkeypatch):
    _use_keys(monkeypatch, OLD)
    db = _db()
    chain = FakeRegistry()
    _released(db, monkeypatch, "61")  # a public track-record sale (sale_ref)
    admin = _admin(db)
    _funded_dispute_case(db, monkeypatch, "62")
    dispute = db.execute(select(Dispute)).scalar_one()
    force_resolve_dispute_case(  # an admin decision (admin_ref) and admin money audit rows
        db,
        dispute.id,
        decision=AdminDecision.SPLIT,
        reason="Both are partly right.",
        actor_id=admin.id,
        seller_share=Decimal("0.6"),
    )
    _anchor_everything(db, chain)
    assert run_full_audit(db, chain.root_of).verdict == "CLEAN"
    return db, chain


def test_records_made_under_the_old_key_audit_clean_after_a_rotation(monkeypatch) -> None:
    db, chain = _anchored_under_the_old_key(monkeypatch)

    _use_keys(monkeypatch, NEW, OLD)
    audit = run_full_audit(db, chain.root_of)

    assert audit.verdict == "CLEAN", [(f.code, f.message) for f in audit.findings]


def test_dropping_the_old_key_is_still_caught(monkeypatch) -> None:
    db, chain = _anchored_under_the_old_key(monkeypatch)

    _use_keys(monkeypatch, NEW)
    codes = {finding.code for finding in run_full_audit(db, chain.root_of).findings}

    assert {"sale_record_mismatch", "admin_decision_changed"} <= codes


def test_a_late_mpesa_row_from_before_the_rotation_is_not_audited_twice(monkeypatch) -> None:
    _use_keys(monkeypatch, OLD)
    db = _db()
    transaction, stk, _card, _pesapal = _switched_order(db, "63")
    late = _callback(stk.checkout_request_id or "", amount=float(transaction.amount))
    assert handle_callback(db, late, client=_daraja(PAID).client()) == "extra_payment"
    (row,) = _audits(db, LATE_MPESA)
    assert row.provider_reference == provider_ref(stk.checkout_request_id)

    _use_keys(monkeypatch, NEW, OLD)
    assert provider_ref(stk.checkout_request_id) != row.provider_reference
    assert row.provider_reference in candidate_refs(stk.checkout_request_id or "")
    record_mpesa_after_card_switch(db, transaction, stk, receipt=None)

    assert len(_audits(db, LATE_MPESA)) == 1
