"""Find and repair escrows whose funding never reached the ledger (issue #222).

Before #222 the eConfirm funding poller set ``escrows.funded_amount`` without a ledger posting.
For every funded escrow, the ESCROW_HELD debits (money in) must equal ``funded_amount``. An escrow
with less on the ledger gets the missing ESCROW_HELD / BUYER_CLEARING movement. An escrow with
more on the ledger than ``funded_amount`` is listed for a person to look at and never changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry, LedgerEntrySide
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event
from app.services.ledger import (
    BUYER_CLEARING_ACCOUNT,
    ESCROW_HELD_ACCOUNT,
    EscrowLedger,
    LedgerMovement,
)

POST = "post"
REVIEW = "review"


@dataclass(frozen=True)
class FundingLedgerGap:
    escrow_id: uuid.UUID
    transaction_id: uuid.UUID
    custody_provider: str | None
    funded_amount: Decimal
    ledger_funded: Decimal
    action: str  # POST (the missing difference) or REVIEW (listed only)

    @property
    def missing(self) -> Decimal:
        return (self.funded_amount - self.ledger_funded).quantize(Decimal("0.01"))


def escrow_held_funded(db: Session, escrow_id: uuid.UUID) -> Decimal:
    """Money recorded into the escrow on the ledger: its ESCROW_HELD debits."""
    total = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.escrow_id == escrow_id,
            LedgerEntry.account_code == ESCROW_HELD_ACCOUNT,
            LedgerEntry.side == LedgerEntrySide.DEBIT,
        )
    ).scalar_one()
    return Decimal(str(total)).quantize(Decimal("0.01"))


def find_funding_ledger_gaps(db: Session) -> list[FundingLedgerGap]:
    escrows = (
        db.execute(select(Escrow).where(Escrow.funded_amount > 0).order_by(Escrow.created_at.asc()))
        .scalars()
        .all()
    )
    gaps: list[FundingLedgerGap] = []
    for escrow in escrows:
        funded = Decimal(escrow.funded_amount).quantize(Decimal("0.01"))
        on_ledger = escrow_held_funded(db, escrow.id)
        if on_ledger == funded:
            continue
        gaps.append(
            FundingLedgerGap(
                escrow_id=escrow.id,
                transaction_id=escrow.transaction_id,
                custody_provider=escrow.custody_provider,
                funded_amount=funded,
                ledger_funded=on_ledger,
                action=POST if on_ledger < funded else REVIEW,
            )
        )
    return gaps


def repair_funding_ledger(db: Session, *, apply: bool) -> list[FundingLedgerGap]:
    """List the gaps; with ``apply``, post each missing difference and commit once."""
    gaps = find_funding_ledger_gaps(db)
    if not apply:
        return gaps

    ledger = EscrowLedger(db)
    repaired = 0
    for gap in gaps:
        if gap.action != POST:
            continue
        escrow = db.get(Escrow, gap.escrow_id)
        currency = escrow.currency if escrow is not None else "KES"
        result = ledger.post_movement(
            LedgerMovement(
                escrow_id=gap.escrow_id,
                idempotency_key=f"repair-funding:{gap.escrow_id}",
                amount=gap.missing,
                debit_account_code=ESCROW_HELD_ACCOUNT,
                credit_account_code=BUYER_CLEARING_ACCOUNT,
                description="Repair: funding recorded without a ledger posting (#222)",
                currency=currency,
            ),
            commit=False,
        )
        if result.created_entries != 2:
            continue
        record_money_audit_event(
            db,
            action="funding_ledger_repaired",
            actor_type=ACTOR_SYSTEM,
            reason="repair-funding-ledger",
            transaction_id=gap.transaction_id,
            escrow_id=gap.escrow_id,
            rail_name=gap.custody_provider,
            amount=gap.missing,
            currency=currency,
            details={
                "funded_amount": str(gap.funded_amount),
                "ledger_funded_before": str(gap.ledger_funded),
            },
        )
        repaired += 1
    if repaired:
        db.commit()
    return gaps
