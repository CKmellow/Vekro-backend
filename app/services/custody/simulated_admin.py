import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.models.collection_attempt import AttemptOutcome, CollectionAttempt
from app.models.escrow import Escrow
from app.models.payout_attempt import PayoutAttempt
from app.services.audit import ACTOR_ADMIN, record_money_audit_event
from app.services.custody.dto import CollectionResult, FundingRequest, PayoutResult
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.simulated_provider import SimulatedCustodyProvider
from app.services.custody.simulated_rail import SimulatedRail
from app.services.ledger import ESCROW_HELD_ACCOUNT, EscrowLedger, LedgerMovement


class SimulatedAdminControlError(Exception):
    pass


class SimulatedAdminAttemptNotFoundError(SimulatedAdminControlError):
    pass


class SimulatedAdminEscrowNotFoundError(SimulatedAdminControlError):
    pass


class SimulatedAdminPayoutTypeError(SimulatedAdminControlError):
    pass


@dataclass(frozen=True)
class SimulatedCollectionAdminResult:
    escrow_reference: str
    provider_reference: str
    outcome: CollectionOutcome
    raw_status: str | None
    message: str | None
    idempotent_replay: bool


@dataclass(frozen=True)
class SimulatedPayoutAdminResult:
    escrow_reference: str
    provider_reference: str
    payout_type: str
    outcome: PayoutOutcome
    raw_status: str | None
    message: str | None
    idempotent_replay: bool


def force_complete_simulated_collection(
    db: Session,
    *,
    escrow_reference: str,
    amount: Decimal,
    phone_number: str,
    account_reference: str | None = None,
    currency: str = "KES",
    actor_id: uuid.UUID | None = None,
) -> SimulatedCollectionAdminResult:
    rail = _build_simulated_rail_from_settings()
    provider = SimulatedCustodyProvider(db, collection_rail=rail, payout_rail=rail)

    forced_account_reference = _force_success_account_reference(
        account_reference=account_reference,
        trigger_prefix=rail.trigger_prefix,
    )
    request = FundingRequest(
        escrow_reference=escrow_reference,
        amount=amount,
        phone_number=phone_number,
        account_reference=forced_account_reference,
        currency=currency,
    )

    preview = rail.request_funding(request)
    existing_attempt = _latest_collection_attempt_by_reference(
        db,
        provider_reference=preview.provider_reference,
    )
    if existing_attempt is not None and existing_attempt.outcome == AttemptOutcome.SUCCEEDED:
        return SimulatedCollectionAdminResult(
            escrow_reference=escrow_reference,
            provider_reference=preview.provider_reference,
            outcome=CollectionOutcome.SUCCEEDED,
            raw_status=existing_attempt.response_snapshot.get("raw_status"),
            message=existing_attempt.response_snapshot.get("message"),
            idempotent_replay=True,
        )

    result = provider.request_funding(request)
    escrow_id = SimulatedCustodyProvider._parse_escrow_reference(escrow_reference)
    escrow = db.get(Escrow, escrow_id)
    record_money_audit_event(
        db,
        action="simulated_collection_forced",
        actor_type=ACTOR_ADMIN,
        actor_id=actor_id,
        reason="Admin forced simulated collection success.",
        transaction_id=escrow.transaction_id if escrow else None,
        escrow_id=escrow_id,
        provider_reference=result.provider_reference,
        rail_name="simulated",
        amount=amount,
        currency=currency,
        details={"outcome": result.outcome.value},
    )
    db.commit()
    return SimulatedCollectionAdminResult(
        escrow_reference=escrow_reference,
        provider_reference=result.provider_reference,
        outcome=result.outcome,
        raw_status=result.raw_status,
        message=result.message,
        idempotent_replay=False,
    )


def progress_simulated_collection_scenario(
    db: Session,
    *,
    provider_reference: str,
    actor_id: uuid.UUID | None = None,
) -> SimulatedCollectionAdminResult:
    attempt = _latest_collection_attempt_by_reference(db, provider_reference=provider_reference)
    if attempt is None:
        raise SimulatedAdminAttemptNotFoundError("Collection attempt was not found.")

    rail = _build_simulated_rail_from_settings()
    status_result = _next_collection_status(
        rail,
        provider_reference=provider_reference,
        attempt=attempt,
    )
    next_outcome = _attempt_outcome_from_collection(status_result.outcome)

    no_status_change = (
        attempt.outcome == next_outcome
        and attempt.response_snapshot.get("raw_status") == status_result.raw_status
        and attempt.response_snapshot.get("message") == status_result.message
    )

    attempt.outcome = next_outcome
    attempt.response_snapshot = status_result.to_payload()
    attempt.failure_code = _failure_code(next_outcome, status_result.raw_status)
    attempt.failure_reason = _failure_reason(next_outcome, status_result.message)
    attempt.attempted_at = datetime.now(UTC)
    db.add(attempt)

    created_entries = 0
    if status_result.outcome == CollectionOutcome.SUCCEEDED:
        escrow = db.get(Escrow, attempt.escrow_id)
        if escrow is None:
            raise SimulatedAdminEscrowNotFoundError("Escrow for collection attempt was not found.")

        ledger_result = EscrowLedger(db).post_movement(
            LedgerMovement(
                escrow_id=escrow.id,
                idempotency_key=f"sim-funding:{provider_reference}",
                amount=attempt.amount,
                debit_account_code=ESCROW_HELD_ACCOUNT,
                credit_account_code="BUYER_CLEARING",
                description="Simulated custody funding (admin progression)",
                currency=attempt.currency,
            )
        )
        created_entries = ledger_result.created_entries
        if created_entries == 2:
            escrow.funded_amount = _quantize(escrow.funded_amount + attempt.amount)
            db.add(escrow)

    _audit_admin_progression(
        db,
        action="simulated_collection_progressed",
        actor_id=actor_id,
        escrow_id=attempt.escrow_id,
        attempt_id=attempt.id,
        provider_reference=provider_reference,
        amount=attempt.amount,
        currency=attempt.currency,
        outcome=status_result.outcome.value,
        ledger_posted=created_entries == 2,
    )
    db.commit()

    return SimulatedCollectionAdminResult(
        escrow_reference=attempt.request_snapshot.get("escrow_reference", ""),
        provider_reference=provider_reference,
        outcome=status_result.outcome,
        raw_status=status_result.raw_status,
        message=status_result.message,
        idempotent_replay=no_status_change and created_entries == 0,
    )


def progress_simulated_payout_scenario(
    db: Session,
    *,
    provider_reference: str,
    actor_id: uuid.UUID | None = None,
) -> SimulatedPayoutAdminResult:
    attempt = _latest_payout_attempt_by_reference(db, provider_reference=provider_reference)
    if attempt is None:
        raise SimulatedAdminAttemptNotFoundError("Payout attempt was not found.")

    payout_type = _infer_payout_type(attempt)
    if payout_type is None:
        raise SimulatedAdminPayoutTypeError(
            "Could not infer payout type from attempt idempotency key."
        )

    rail = _build_simulated_rail_from_settings()
    status_result = _next_payout_status(
        rail,
        provider_reference=provider_reference,
        attempt=attempt,
    )
    next_outcome = _attempt_outcome_from_payout(status_result.outcome)

    no_status_change = (
        attempt.outcome == next_outcome
        and attempt.response_snapshot.get("raw_status") == status_result.raw_status
        and attempt.response_snapshot.get("message") == status_result.message
    )

    attempt.outcome = next_outcome
    attempt.response_snapshot = status_result.to_payload()
    attempt.failure_code = _failure_code(next_outcome, status_result.raw_status)
    attempt.failure_reason = _failure_reason(next_outcome, status_result.message)
    attempt.attempted_at = datetime.now(UTC)
    db.add(attempt)

    created_entries = 0
    if status_result.outcome == PayoutOutcome.SUCCEEDED:
        escrow = db.get(Escrow, attempt.escrow_id)
        if escrow is None:
            raise SimulatedAdminEscrowNotFoundError("Escrow for payout attempt was not found.")

        debit_account_code = "SELLER_PAYABLE" if payout_type == "release" else "BUYER_REFUNDABLE"
        ledger_result = EscrowLedger(db).post_movement(
            LedgerMovement(
                escrow_id=escrow.id,
                idempotency_key=f"sim-{payout_type}:{provider_reference}",
                amount=attempt.amount,
                debit_account_code=debit_account_code,
                credit_account_code=ESCROW_HELD_ACCOUNT,
                description=f"Simulated custody {payout_type} (admin progression)",
                currency=attempt.currency,
            )
        )
        created_entries = ledger_result.created_entries
        if created_entries == 2:
            if payout_type == "release":
                escrow.released_amount = _quantize(escrow.released_amount + attempt.amount)
            else:
                escrow.refunded_amount = _quantize(escrow.refunded_amount + attempt.amount)
            db.add(escrow)

    _audit_admin_progression(
        db,
        action=f"simulated_payout_{payout_type}_progressed",
        actor_id=actor_id,
        escrow_id=attempt.escrow_id,
        attempt_id=attempt.id,
        provider_reference=provider_reference,
        amount=attempt.amount,
        currency=attempt.currency,
        outcome=status_result.outcome.value,
        ledger_posted=created_entries == 2,
    )
    db.commit()

    return SimulatedPayoutAdminResult(
        escrow_reference=attempt.request_snapshot.get("escrow_reference", ""),
        provider_reference=provider_reference,
        payout_type=payout_type,
        outcome=status_result.outcome,
        raw_status=status_result.raw_status,
        message=status_result.message,
        idempotent_replay=no_status_change and created_entries == 0,
    )


def _audit_admin_progression(
    db: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    escrow_id: uuid.UUID,
    attempt_id: uuid.UUID,
    provider_reference: str,
    amount: Decimal,
    currency: str,
    outcome: str,
    ledger_posted: bool,
) -> None:
    escrow = db.get(Escrow, escrow_id)
    record_money_audit_event(
        db,
        action=action,
        actor_type=ACTOR_ADMIN,
        actor_id=actor_id,
        reason="Admin progressed simulated provider scenario.",
        transaction_id=escrow.transaction_id if escrow else None,
        escrow_id=escrow_id,
        attempt_id=attempt_id,
        provider_reference=provider_reference,
        rail_name="simulated",
        amount=amount,
        currency=currency,
        details={"outcome": outcome, "ledger_posted": ledger_posted},
    )


def _build_simulated_rail_from_settings() -> SimulatedRail:
    settings = get_settings()
    return SimulatedRail(
        collection_default_scenario=settings.simulated_collection_default_scenario,
        payout_default_scenario=settings.simulated_payout_default_scenario,
        trigger_prefix=settings.simulated_trigger_prefix,
    )


def _force_success_account_reference(*, account_reference: str | None, trigger_prefix: str) -> str:
    base_reference = account_reference.strip() if account_reference else "admin-force-collection"
    if not base_reference:
        base_reference = "admin-force-collection"
    return f"{trigger_prefix}success {base_reference}"


def _latest_collection_attempt_by_reference(
    db: Session,
    *,
    provider_reference: str,
) -> CollectionAttempt | None:
    return (
        db.execute(
            select(CollectionAttempt)
            .where(CollectionAttempt.provider_reference == provider_reference)
            .order_by(CollectionAttempt.attempted_at.desc(), CollectionAttempt.created_at.desc())
        )
        .scalars()
        .first()
    )


def _latest_payout_attempt_by_reference(
    db: Session,
    *,
    provider_reference: str,
) -> PayoutAttempt | None:
    return (
        db.execute(
            select(PayoutAttempt)
            .where(PayoutAttempt.provider_reference == provider_reference)
            .order_by(PayoutAttempt.attempted_at.desc(), PayoutAttempt.created_at.desc())
        )
        .scalars()
        .first()
    )


def _infer_payout_type(attempt: PayoutAttempt) -> str | None:
    if attempt.idempotency_key.startswith("sim-release:"):
        return "release"
    if attempt.idempotency_key.startswith("sim-refund:"):
        return "refund"
    return None


def _attempt_outcome_from_collection(outcome: CollectionOutcome) -> AttemptOutcome:
    if outcome == CollectionOutcome.SUCCEEDED:
        return AttemptOutcome.SUCCEEDED
    if outcome == CollectionOutcome.FAILED_DEFINITE:
        return AttemptOutcome.FAILED_DEFINITE
    return AttemptOutcome.UNKNOWN


def _attempt_outcome_from_payout(outcome: PayoutOutcome) -> AttemptOutcome:
    if outcome == PayoutOutcome.SUCCEEDED:
        return AttemptOutcome.SUCCEEDED
    if outcome == PayoutOutcome.FAILED_DEFINITE:
        return AttemptOutcome.FAILED_DEFINITE
    return AttemptOutcome.UNKNOWN


def _failure_code(outcome: AttemptOutcome, raw_status: str | None) -> str | None:
    if outcome == AttemptOutcome.SUCCEEDED:
        return None
    return raw_status or outcome.value


def _failure_reason(outcome: AttemptOutcome, message: str | None) -> str | None:
    if outcome == AttemptOutcome.SUCCEEDED:
        return None
    return message


def _quantize(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _next_collection_status(
    rail: SimulatedRail,
    *,
    provider_reference: str,
    attempt: CollectionAttempt,
) -> CollectionResult:
    if _scenario_from_provider_reference(provider_reference) != "out_of_order":
        return rail.get_funding_status(provider_reference)

    prior_status = str(attempt.response_snapshot.get("raw_status") or "")
    if prior_status in {"out_of_order_pending", "settled_after_out_of_order"}:
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="settled_after_out_of_order",
            message="Simulated rail reconciled previously out-of-order flow.",
        )

    return CollectionResult(
        outcome=CollectionOutcome.UNKNOWN,
        provider_reference=provider_reference,
        raw_status="out_of_order_pending",
        message="Simulated rail awaiting reconciliation after out-of-order callback.",
    )


def _next_payout_status(
    rail: SimulatedRail,
    *,
    provider_reference: str,
    attempt: PayoutAttempt,
) -> PayoutResult:
    if _scenario_from_provider_reference(provider_reference) != "out_of_order":
        return rail.get_payout_status(provider_reference)

    prior_status = str(attempt.response_snapshot.get("raw_status") or "")
    if prior_status in {"out_of_order_pending", "settled_after_out_of_order"}:
        return PayoutResult(
            outcome=PayoutOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="settled_after_out_of_order",
            message="Simulated rail reconciled previously out-of-order flow.",
        )

    return PayoutResult(
        outcome=PayoutOutcome.UNKNOWN,
        provider_reference=provider_reference,
        raw_status="out_of_order_pending",
        message="Simulated rail awaiting reconciliation after out-of-order callback.",
    )


def _scenario_from_provider_reference(provider_reference: str) -> str:
    parts = provider_reference.split(":")
    if len(parts) >= 4 and parts[0] == "sim":
        return parts[2].strip().lower()
    return ""
