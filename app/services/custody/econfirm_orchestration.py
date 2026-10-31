"""E5 orchestration: open-on-eConfirm, stickiness persistence, and the poller.

This is the custody machinery; E7 calls it from transaction creation
(``app.services.transaction.create_transaction``). It provides:

* ``open_econfirm_escrow`` - open a new escrow on eConfirm, persist the sticky
  provider, the provider txn id, the encrypted confirmation code and the account
  cap; handle UNKNOWN (needs_reconciliation, no STK) and definite rejection
  (honest fallback to simulated, allowed only because no provider call has
  succeeded yet, Hard Rule 1).
* ``request_econfirm_funding`` - send the eConfirm STK prompt for an opened
  escrow and record the payer phone. A sent prompt is never "funded".
* ``run_econfirm_funding_reconciliation`` - ONE shared, budgeted, timeout-bounded
  poller that confirms funding only via GET (Hard Rule 4 / A2). User-facing reads
  must use the DB, never this.
* ``next_poll_delay_seconds`` - jittered 10s->60s backoff for the scheduler (A2).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.escrow import Escrow
from app.services.audit import ACTOR_SYSTEM, record_money_audit_event
from app.services.custody.dto import CollectionResult, FundingRequest, OpenEscrowRequest
from app.services.custody.econfirm_crypto import build_cipher
from app.services.custody.econfirm_provider import (
    EconfirmDefiniteError,
    EconfirmUnknownError,
)
from app.services.custody.enums import CollectionOutcome
from app.services.custody.selection import (
    PROVIDER_ECONFIRM,
    PROVIDER_SIMULATED,
    effective_max_principal,
)
from app.services.ledger import post_escrow_funding

_logger = logging.getLogger("app.custody.econfirm.orchestration")

_POLLABLE_FUNDING_BUDGET = 50  # per run; keeps us well under ~600/hr with jittered runs


@dataclass(frozen=True)
class EconfirmPollResult:
    polled: int
    funded: int
    skipped_timed_out: int
    budget: int


def open_econfirm_escrow(
    db: Session,
    escrow: Escrow,
    *,
    provider,
    settings,
    buyer_email: str | None,
    seller_email: str | None,
    receiver_phone: str,
) -> str:
    """Open ``escrow`` on eConfirm and persist the sticky provider state.

    Returns the custody provider the escrow ended up on ('econfirm' or, on a
    definite rejection, 'simulated'). Never sends an STK push here.
    """
    request = OpenEscrowRequest(
        transaction_id=str(escrow.transaction_id),
        buyer_id=str(escrow.buyer_id),
        seller_id=str(escrow.seller_id),
        amount=escrow.amount_total,
        currency=escrow.currency,
        buyer_email=buyer_email,
        seller_email=seller_email,
        receiver_phone=receiver_phone,
    )

    try:
        record = provider.open_escrow(request)
    except EconfirmUnknownError:
        # The escrow MAY exist at eConfirm; do not retry blindly and do not send
        # an STK. Flag for manual reconciliation and keep it on eConfirm (sticky).
        escrow.custody_provider = PROVIDER_ECONFIRM
        escrow.econfirm_needs_reconciliation = True
        escrow.econfirm_transaction_id = None
        db.add(escrow)
        record_money_audit_event(
            db,
            action="econfirm_open_unknown_needs_reconciliation",
            actor_type=ACTOR_SYSTEM,
            transaction_id=escrow.transaction_id,
            escrow_id=escrow.id,
            rail_name=PROVIDER_ECONFIRM,
            amount=escrow.amount_total,
            currency=escrow.currency,
            reason="create_returned_unknown",
        )
        _logger.warning(
            "econfirm_open_unknown escrow_id=%s txn=%s -> needs_reconciliation",
            escrow.id,
            escrow.transaction_id,
        )
        return PROVIDER_ECONFIRM
    except (EconfirmDefiniteError, ValueError):
        # No provider call has succeeded yet, so honest fallback to simulated is
        # permitted (Hard Rule 1).
        escrow.custody_provider = PROVIDER_SIMULATED
        db.add(escrow)
        record_money_audit_event(
            db,
            action="econfirm_open_rejected_fallback_simulated",
            actor_type=ACTOR_SYSTEM,
            transaction_id=escrow.transaction_id,
            escrow_id=escrow.id,
            rail_name=PROVIDER_SIMULATED,
            amount=escrow.amount_total,
            currency=escrow.currency,
            reason="econfirm_open_rejected",
        )
        _logger.warning("econfirm_open_rejected escrow_id=%s -> simulated", escrow.id)
        return PROVIDER_SIMULATED

    # Success: persist the sticky provider, provider reference, encrypted code
    # and account cap. The plaintext code is used only to encrypt, never stored.
    escrow.custody_provider = PROVIDER_ECONFIRM
    escrow.econfirm_transaction_id = record.escrow_reference
    escrow.econfirm_needs_reconciliation = False
    if record.confirmation_code:
        cipher = build_cipher(settings.econfirm_code_encryption_key)
        escrow.econfirm_confirmation_code = cipher.encrypt(record.confirmation_code)
    db.add(escrow)

    cap = effective_max_principal(settings, record.max_principal_kes)
    record_money_audit_event(
        db,
        action="econfirm_open_succeeded",
        actor_type=ACTOR_SYSTEM,
        transaction_id=escrow.transaction_id,
        escrow_id=escrow.id,
        provider_reference=record.escrow_reference,
        rail_name=PROVIDER_ECONFIRM,
        amount=escrow.amount_total,
        currency=escrow.currency,
        reason="econfirm_open_succeeded",
        details={"effective_max_principal_kes": str(cap)},
    )
    return PROVIDER_ECONFIRM


def request_econfirm_funding(
    db: Session,
    escrow: Escrow,
    *,
    provider,
    payer_phone: str,
    account_reference: str,
) -> CollectionResult:
    """Send the eConfirm STK prompt for an escrow that opened cleanly.

    Records the payer phone (reversal returns funds there, A5). The result is
    only "prompt sent" or a definite rejection: funding is confirmed solely by
    the poller (Hard Rule 4), so this never touches ``funded_amount``.
    """
    if (
        escrow.custody_provider != PROVIDER_ECONFIRM
        or not escrow.econfirm_transaction_id
        or escrow.econfirm_needs_reconciliation
    ):
        raise ValueError("Escrow is not ready for eConfirm funding.")

    request = FundingRequest(
        escrow_reference=escrow.econfirm_transaction_id,
        amount=escrow.amount_total,
        phone_number=payer_phone,
        account_reference=account_reference,
        currency=escrow.currency,
    )
    escrow.econfirm_payer_phone = request.phone_number
    db.add(escrow)

    result = provider.request_funding(request)
    record_money_audit_event(
        db,
        action="econfirm_stk_requested",
        actor_type=ACTOR_SYSTEM,
        transaction_id=escrow.transaction_id,
        escrow_id=escrow.id,
        provider_reference=escrow.econfirm_transaction_id,
        rail_name=PROVIDER_ECONFIRM,
        amount=escrow.amount_total,
        currency=escrow.currency,
        reason=result.outcome.value,
        details={"raw_status": result.raw_status},
    )
    return result


def _lock_funded_transaction(db: Session, escrow: Escrow) -> None:
    # Imported lazily: the transaction service imports this module.
    from app.services.transaction import lock_transaction_after_custody_funding

    lock_transaction_after_custody_funding(
        db,
        escrow.transaction_id,
        provider_reference=escrow.econfirm_transaction_id,
        rail_name=PROVIDER_ECONFIRM,
    )


def run_econfirm_funding_reconciliation(
    db: Session,
    *,
    collection_rail,
    now: datetime | None = None,
    max_requests: int = _POLLABLE_FUNDING_BUDGET,
    payment_timeout_minutes: int = 15,
    on_funded: Callable[[Session, Escrow], None] | None = None,
) -> EconfirmPollResult:
    """Confirm eConfirm funding via GET for escrows awaiting payment.

    Polls only eConfirm escrows that have a provider reference, are not flagged
    for reconciliation, and are not yet funded. Bounded by ``max_requests`` and a
    payment timeout (older escrows stop being polled). Funding is confirmed only
    when the provider reports it (Hard Rule 4); no STK is ever sent here.

    Each newly funded escrow is handed to ``on_funded`` (default: move its
    transaction from awaiting_payment to locked, in the same commit).
    """
    funded_hook = on_funded or _lock_funded_transaction
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(minutes=payment_timeout_minutes)

    candidates = list(
        db.execute(
            select(Escrow)
            .where(
                Escrow.custody_provider == PROVIDER_ECONFIRM,
                Escrow.econfirm_transaction_id.is_not(None),
                Escrow.econfirm_needs_reconciliation.is_(False),
                Escrow.funded_amount <= Decimal("0.00"),
            )
            .order_by(Escrow.created_at.asc())
            .limit(max_requests)
        )
        .scalars()
        .all()
    )

    polled = 0
    funded = 0
    skipped = 0
    for escrow in candidates:
        created = escrow.created_at or current
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            # Past the payment window: stop polling (the timeout job handles it).
            skipped += 1
            continue

        polled += 1
        result = collection_rail.get_funding_status(escrow.econfirm_transaction_id)
        if result.outcome == CollectionOutcome.SUCCEEDED:
            # Ledger and funded_amount together, committed with the lock below (#222).
            post_escrow_funding(
                db,
                escrow,
                idempotency_key=f"econfirm-funding:{escrow.id}",
                description="eConfirm custody funding",
            )
            funded += 1
            record_money_audit_event(
                db,
                action="econfirm_funding_confirmed",
                actor_type=ACTOR_SYSTEM,
                transaction_id=escrow.transaction_id,
                escrow_id=escrow.id,
                provider_reference=escrow.econfirm_transaction_id,
                rail_name=PROVIDER_ECONFIRM,
                amount=escrow.amount_total,
                currency=escrow.currency,
                reason="poller_confirmed_funding",
            )
            funded_hook(db, escrow)

    db.commit()
    return EconfirmPollResult(
        polled=polled, funded=funded, skipped_timed_out=skipped, budget=max_requests
    )


def next_poll_delay_seconds(
    attempt_index: int,
    *,
    base_seconds: float = 10.0,
    cap_seconds: float = 60.0,
    rng: random.Random | None = None,
) -> float:
    """Jittered exponential backoff for the poll scheduler, in [base, cap] (A2)."""
    generator = rng or random
    exponential = base_seconds * (2 ** max(0, attempt_index))
    ceiling = min(cap_seconds, exponential)
    low = min(base_seconds, ceiling)
    return generator.uniform(low, ceiling)
