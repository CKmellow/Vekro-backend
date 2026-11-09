"""Reserves reconciliation (M24 B2, #269; docs/anchoring.md "Reserves").

Liabilities (``liabilities.py``) say what Vekro owes buyers. This compares, per custody provider,
that liability with what the provider says it holds, and anchors the result as a
``reserves_reconciliation`` event in the global chain:

- eConfirm has no balance endpoint, so each open eConfirm escrow's current status is asked for;
  only escrows eConfirm reports as funded count as attested. Unreachable answers make the result
  ``incomplete``, never a shortfall.
- Daraja (M-Pesa) and Pesapal collect into the merchant's own account; their balance is not
  available here (Daraja's Account Balance was not verified in the sandbox, Pesapal has no balance
  API), so the result is ``unavailable``.
- Simulated escrows move no real money: ``not_applicable``.

A definite shortfall is an admin alert: an anchored ``reserves_shortfall`` money audit event, a
warning log line and the admin anchoring status page. Provider answers are unsigned, so this
cannot prove the provider's figure is true (docs/anchoring.md).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.escrow import Escrow
from app.models.transaction import Transaction
from app.services.anchoring import payloads
from app.services.anchoring.recorder import record_anchor_event
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome

_logger = logging.getLogger("app.anchoring.reserves")

OK, SHORTFALL, INCOMPLETE, UNAVAILABLE, NOT_APPLICABLE = (
    "covered",
    "shortfall",
    "incomplete",
    "unavailable",
    "not_applicable",
)
_UNAVAILABLE_METHODS = {
    "daraja": "unavailable: M-Pesa Account Balance not verified in the sandbox",
    "pesapal": "unavailable: Pesapal has no balance API",
}


@dataclass(frozen=True)
class ReservesResult:
    provider: str
    method: str
    status: str
    liability: Decimal
    attested: Decimal | None
    escrows: int


def _held(escrow: Escrow) -> Decimal:
    return Decimal(escrow.funded_amount - escrow.released_amount - escrow.refunded_amount)


def _provider(escrow: Escrow, transaction: Transaction) -> str:
    if (escrow.custody_provider or "").lower() == "econfirm":
        return "econfirm"
    return transaction.collection_rail or "simulated"


def reconcile_reserves(
    db: Session,
    *,
    econfirm_status_of: Callable[[str], CollectionResult] | None = None,
    now: datetime | None = None,
) -> list[ReservesResult]:
    at = now or datetime.now(UTC)
    rows = db.execute(
        select(Escrow, Transaction)
        .join(Transaction, Transaction.id == Escrow.transaction_id)
        .where(Escrow.funded_amount > Escrow.released_amount + Escrow.refunded_amount)
    ).all()
    groups: dict[str, list[Escrow]] = defaultdict(list)
    for escrow, transaction in rows:
        groups[_provider(escrow, transaction)].append(escrow)

    results = []
    for provider in sorted(groups):
        escrows = groups[provider]
        liability = sum((_held(e) for e in escrows), Decimal("0"))
        if provider == "econfirm":
            result = _econfirm(escrows, liability, econfirm_status_of)
        elif provider == "simulated":
            result = ReservesResult(
                provider,
                "simulated ledger: no real money",
                NOT_APPLICABLE,
                liability,
                None,
                len(escrows),
            )
        else:
            result = ReservesResult(
                provider,
                _UNAVAILABLE_METHODS.get(provider, "unavailable"),
                UNAVAILABLE,
                liability,
                None,
                len(escrows),
            )
        _record(db, result, at)
        results.append(result)
    db.commit()
    return results


def _econfirm(
    escrows: list[Escrow],
    liability: Decimal,
    status_of: Callable[[str], CollectionResult] | None,
) -> ReservesResult:
    method = "econfirm per-escrow status (no balance endpoint)"
    if status_of is None:
        return ReservesResult(
            "econfirm",
            method + "; eConfirm not configured",
            UNAVAILABLE,
            liability,
            None,
            len(escrows),
        )
    attested, unknown = Decimal("0"), 0
    for escrow in escrows:
        reference = escrow.econfirm_transaction_id
        try:
            answer = status_of(reference) if reference else None
        except Exception:  # an unreachable provider is not a shortfall
            answer = None
        if answer is None or answer.outcome == CollectionOutcome.UNKNOWN:
            unknown += 1
        elif answer.outcome == CollectionOutcome.SUCCEEDED:
            attested += _held(escrow)
    if unknown:
        status = INCOMPLETE
    else:
        status = OK if attested >= liability else SHORTFALL
    return ReservesResult("econfirm", method, status, liability, attested, len(escrows))


def _record(db: Session, result: ReservesResult, at: datetime) -> None:
    record_anchor_event(
        db,
        event_type=payloads.RESERVES_RECONCILIATION,
        transaction_id=None,
        payload=payloads.reserves_reconciliation_payload(
            at=at,
            provider=result.provider,
            method=result.method,
            status=result.status,
            liability=result.liability,
            attested=result.attested,
            escrows=result.escrows,
        ),
    )
    if result.status == SHORTFALL and result.attested is not None:
        shortfall = result.liability - result.attested
        _logger.warning(
            "reserves_shortfall provider=%s liability=%s attested=%s",
            result.provider,
            result.liability,
            result.attested,
        )
        record_money_audit_event(
            db,
            action="reserves_shortfall",
            actor_type=ACTOR_SYSTEM,
            reason=f"{result.provider} attests less than Vekro owes buyers.",
            rail_name=result.provider,
            amount=shortfall,
            currency="KES",
            details={"liability": str(result.liability), "attested": str(result.attested)},
            occurred_at=at,
        )
