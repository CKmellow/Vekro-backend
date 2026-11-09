"""#269: reserves reconciliation per custody provider, anchored, with a shortfall alert."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app.models.anchor_event import AnchorEvent
from app.models.escrow import Escrow
from app.models.money_audit_event import MoneyAuditEvent
from app.services.anchoring.reserves import reconcile_reserves
from app.services.anchoring.status import anchoring_status
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from sqlalchemy import select

from tests.test_liabilities import _funded, _with_tables


def _as_econfirm(db, transaction, reference: str) -> Escrow:
    escrow = db.execute(select(Escrow).where(Escrow.transaction_id == transaction.id)).scalar_one()
    escrow.custody_provider = "econfirm"
    escrow.econfirm_transaction_id = reference
    db.commit()
    return escrow


def _answer(outcome: CollectionOutcome):
    return lambda reference: CollectionResult(
        outcome=outcome, provider_reference=reference, raw_status="x"
    )


def _payloads(db) -> list[dict]:
    rows = db.execute(
        select(AnchorEvent).where(AnchorEvent.event_type == "reserves_reconciliation")
    ).scalars()
    return [json.loads(r.payload) for r in rows]


def _status(db):
    settings = SimpleNamespace(
        anchor_enabled=False,
        anchor_chain_id=31337,
        anchor_registry_address="",
        anchor_private_key="",
        anchor_gate_payouts=False,
    )
    return anchoring_status(db, settings)


def test_nothing_held_records_nothing() -> None:
    db = _with_tables()
    assert reconcile_reserves(db) == []


def test_card_money_is_unavailable_not_a_shortfall() -> None:
    db = _with_tables()
    order = _funded(db, "71")

    (result,) = reconcile_reserves(db)

    assert (result.provider, result.status) == ("pesapal", "unavailable")
    assert result.liability == order.amount and result.attested is None
    (payload,) = _payloads(db)
    assert payload["attested_total"] is None and payload["liability_total"] == f"{order.amount:.2f}"
    assert str(order.id) not in json.dumps(payload)


def test_econfirm_covered_when_every_escrow_is_funded() -> None:
    db = _with_tables()
    order = _funded(db, "72")
    _as_econfirm(db, order, "ec-72")

    (result,) = reconcile_reserves(db, econfirm_status_of=_answer(CollectionOutcome.SUCCEEDED))

    assert (result.provider, result.status) == ("econfirm", "covered")
    assert result.attested == result.liability == order.amount
    assert not _status(db).reserves_shortfall


def test_econfirm_shortfall_is_anchored_and_raised() -> None:
    db = _with_tables()
    order = _funded(db, "73")
    _as_econfirm(db, order, "ec-73")

    (result,) = reconcile_reserves(
        db, econfirm_status_of=_answer(CollectionOutcome.FAILED_DEFINITE)
    )

    assert result.status == "shortfall" and result.attested == 0
    (payload,) = _payloads(db)
    assert payload["difference"] == f"{-order.amount:.2f}"
    alert = db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "reserves_shortfall")
    ).scalar_one()
    assert alert.amount == order.amount and alert.rail_name == "econfirm"
    status = _status(db)
    assert status.reserves_shortfall
    assert [(r.provider, r.status) for r in status.reserves] == [("econfirm", "shortfall")]


@pytest.mark.parametrize("status_of", [None, "boom"], ids=["not configured", "unreachable"])
def test_econfirm_without_an_answer_is_never_a_shortfall(status_of) -> None:
    db = _with_tables()
    order = _funded(db, "74")
    _as_econfirm(db, order, "ec-74")

    def unreachable(reference):
        raise RuntimeError("timeout")

    (result,) = reconcile_reserves(
        db, econfirm_status_of=unreachable if status_of == "boom" else None
    )

    assert result.status in ("unavailable", "incomplete")
    assert not db.execute(
        select(MoneyAuditEvent).where(MoneyAuditEvent.action == "reserves_shortfall")
    ).first()


def test_status_page_shows_the_latest_liabilities_snapshot() -> None:
    from app.services.anchoring.liabilities import build_liabilities_snapshot

    db = _with_tables()
    order = _funded(db, "75")
    build_liabilities_snapshot(db)
    reconcile_reserves(db)

    status = _status(db)
    assert status.liabilities is not None
    assert status.liabilities.total == f"{order.amount:.2f}" and status.liabilities.leaf_count == 1
