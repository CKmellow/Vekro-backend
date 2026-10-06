from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.models.collection_attempt import AttemptOutcome
from app.models.escrow import Escrow
from app.models.notification import Notification, NotificationEventType
from app.models.payout_attempt import PayoutAttempt
from app.models.transaction import Transaction, TransactionPayoutStatus, TransactionStatus
from app.models.user import User, UserRole
from app.services.custody.dto import PayoutRequest, PayoutResult
from app.services.custody.enums import PayoutOutcome
from app.services.custody.rail_breaker import (
    record_rail_failure,
    record_rail_success,
    select_payout_rail_for_routing,
)
from app.services.custody.registry import CustodyRegistry, build_custody_registry
from app.services.ledger import ESCROW_HELD_ACCOUNT, EscrowLedger, LedgerMovement


class EscrowServiceError(Exception):
    pass


class SplitPayoutUnsupportedError(EscrowServiceError):
    pass


@dataclass(frozen=True)
class PayoutExecutorRunResult:
    claimed: int
    processed: int
    succeeded: int
    failed_definite: int
    unknown: int


@dataclass(frozen=True)
class PayoutReconciliationResult:
    unknown_alerts: int
    mismatch_alerts: int


class EscrowService:
    def __init__(self, db: Session, *, registry: CustodyRegistry | None = None) -> None:
        self._db = db
        self._registry = registry or build_custody_registry(get_settings())
        self._escrow_cache: dict[uuid.UUID, Escrow] = {}

    @property
    def registry(self) -> CustodyRegistry:
        return self._registry

    def outbox_ready(self) -> bool:
        if not hasattr(self._db, "get_bind"):
            return False

        bind = self._db.get_bind()
        inspector = inspect(bind)
        required = {"escrows", "payout_attempts", "users", "transactions"}
        return required.issubset(set(inspector.get_table_names()))

    def queue_release_full(self, transaction: Transaction, *, purpose: str) -> PayoutAttempt | None:
        return self._enqueue_payout_intent(
            transaction=transaction,
            purpose=purpose,
            payout_kind="release",
            destination_user_id=transaction.seller_id,
            amount=transaction.amount,
        )

    def queue_refund_full(self, transaction: Transaction, *, purpose: str) -> PayoutAttempt | None:
        return self._enqueue_payout_intent(
            transaction=transaction,
            purpose=purpose,
            payout_kind="refund",
            destination_user_id=transaction.buyer_id,
            amount=transaction.amount,
        )

    def queue_split_payout(
        self,
        transaction: Transaction,
        *,
        release_purpose: str,
        refund_purpose: str,
        split_ratio: Decimal,
    ) -> tuple[PayoutAttempt | None, PayoutAttempt | None]:
        if not self._is_split_capability_supported():
            raise SplitPayoutUnsupportedError(
                "Current custody capabilities do not support split payout orchestration."
            )

        ratio = self._normalize_ratio(split_ratio)
        release_amount = (transaction.amount * ratio).quantize(Decimal("0.01"))
        refund_amount = (transaction.amount - release_amount).quantize(Decimal("0.01"))

        if release_amount <= Decimal("0.00") or refund_amount <= Decimal("0.00"):
            raise EscrowServiceError("Split payout amounts must both be greater than zero.")

        release_attempt = self._enqueue_payout_intent(
            transaction=transaction,
            purpose=release_purpose,
            payout_kind="release",
            destination_user_id=transaction.seller_id,
            amount=release_amount,
        )
        refund_attempt = self._enqueue_payout_intent(
            transaction=transaction,
            purpose=refund_purpose,
            payout_kind="refund",
            destination_user_id=transaction.buyer_id,
            amount=refund_amount,
        )

        self.refresh_transaction_payout_status(transaction)
        return release_attempt, refund_attempt

    def refresh_transaction_payout_status(
        self,
        transaction: Transaction,
    ) -> TransactionPayoutStatus:
        if not self.outbox_ready():
            return transaction.payout_status

        escrow = self._find_escrow_for_transaction(transaction.id)
        if escrow is None:
            transaction.payout_status = TransactionPayoutStatus.NOT_REQUIRED
            return transaction.payout_status

        persisted_attempts = list(
            self._db.execute(
                select(PayoutAttempt)
                .where(PayoutAttempt.escrow_id == escrow.id)
                .order_by(PayoutAttempt.created_at.desc(), PayoutAttempt.attempted_at.desc())
            )
            .scalars()
            .all()
        )

        pending_attempts = [
            pending
            for pending in self._db.new
            if isinstance(pending, PayoutAttempt) and pending.escrow_id == escrow.id
        ]

        attempts = pending_attempts + persisted_attempts

        if not attempts:
            transaction.payout_status = TransactionPayoutStatus.NOT_REQUIRED
            return transaction.payout_status

        latest_by_purpose: dict[str, PayoutAttempt] = {}
        for attempt in attempts:
            if attempt.purpose not in latest_by_purpose:
                latest_by_purpose[attempt.purpose] = attempt

        latest_attempts = list(latest_by_purpose.values())

        has_pending = any(
            attempt.outcome == AttemptOutcome.UNKNOWN and attempt.provider_reference is None
            for attempt in latest_attempts
        )
        has_unknown = any(
            attempt.outcome == AttemptOutcome.UNKNOWN and attempt.provider_reference is not None
            for attempt in latest_attempts
        )
        has_failed = any(
            attempt.outcome == AttemptOutcome.FAILED_DEFINITE for attempt in latest_attempts
        )
        all_succeeded = all(
            attempt.outcome == AttemptOutcome.SUCCEEDED for attempt in latest_attempts
        )

        if has_pending:
            transaction.payout_status = TransactionPayoutStatus.PENDING
        elif has_unknown:
            transaction.payout_status = TransactionPayoutStatus.UNKNOWN
        elif all_succeeded:
            transaction.payout_status = TransactionPayoutStatus.SUCCEEDED
        elif has_failed:
            transaction.payout_status = TransactionPayoutStatus.FAILED_DEFINITE
        else:
            transaction.payout_status = TransactionPayoutStatus.PENDING

        return transaction.payout_status

    def _enqueue_payout_intent(
        self,
        *,
        transaction: Transaction,
        purpose: str,
        payout_kind: str,
        destination_user_id: uuid.UUID,
        amount: Decimal,
    ) -> PayoutAttempt | None:
        normalized_purpose = purpose.strip()
        if not normalized_purpose:
            raise EscrowServiceError("Payout purpose is required.")

        payout_amount = self._normalize_amount(amount)

        if not self.outbox_ready():
            return None

        escrow = self._ensure_escrow_for_transaction(transaction)

        existing = (
            self._db.execute(
                select(PayoutAttempt)
                .where(
                    PayoutAttempt.escrow_id == escrow.id,
                    PayoutAttempt.purpose == normalized_purpose,
                    PayoutAttempt.outcome != AttemptOutcome.FAILED_DEFINITE,
                )
                .order_by(PayoutAttempt.created_at.desc(), PayoutAttempt.attempted_at.desc())
            )
            .scalars()
            .first()
        )

        if existing is None:
            for pending in self._db.new:
                if not isinstance(pending, PayoutAttempt):
                    continue
                if pending.escrow_id != escrow.id:
                    continue
                if pending.purpose != normalized_purpose:
                    continue
                if pending.outcome == AttemptOutcome.FAILED_DEFINITE:
                    continue
                existing = pending
                break

        if existing is not None:
            self.refresh_transaction_payout_status(transaction)
            return existing

        destination_phone = self._resolve_destination_phone(destination_user_id)
        rail_name = self._default_payout_rail_name()
        attempt = PayoutAttempt(
            id=uuid.uuid4(),
            escrow_id=escrow.id,
            purpose=normalized_purpose,
            rail_name=rail_name,
            provider_name=rail_name,
            idempotency_key=f"outbox:{normalized_purpose}",
            provider_reference=None,
            amount=payout_amount,
            currency=escrow.currency,
            outcome=AttemptOutcome.UNKNOWN,
            request_snapshot={
                "transaction_id": str(transaction.id),
                "escrow_id": str(escrow.id),
                "payout_kind": payout_kind,
                "purpose": normalized_purpose,
                "destination_phone": destination_phone,
                "amount": str(payout_amount),
                "currency": escrow.currency,
            },
            response_snapshot={},
            failure_code=None,
            failure_reason=None,
        )
        self._db.add(attempt)

        transaction.payout_status = TransactionPayoutStatus.PENDING
        self._db.add(transaction)

        return attempt

    def _ensure_escrow_for_transaction(self, transaction: Transaction) -> Escrow:
        cached = self._escrow_cache.get(transaction.id)
        if cached is not None:
            return cached

        existing = self._find_escrow_for_transaction(transaction.id)
        if existing is not None:
            self._escrow_cache[transaction.id] = existing
            return existing

        escrow = Escrow(
            transaction_id=transaction.id,
            buyer_id=transaction.buyer_id,
            seller_id=transaction.seller_id,
            amount_total=self._normalize_amount(transaction.amount),
            currency="KES",
        )
        self._db.add(escrow)
        self._db.flush()
        self._escrow_cache[transaction.id] = escrow
        return escrow

    def _find_escrow_for_transaction(self, transaction_id: uuid.UUID) -> Escrow | None:
        cached = self._escrow_cache.get(transaction_id)
        if cached is not None:
            return cached

        for pending in self._db.new:
            if not isinstance(pending, Escrow):
                continue
            if pending.transaction_id == transaction_id:
                self._escrow_cache[transaction_id] = pending
                return pending

        for loaded in self._db.identity_map.values():
            if not isinstance(loaded, Escrow):
                continue
            if loaded.transaction_id == transaction_id:
                self._escrow_cache[transaction_id] = loaded
                return loaded

        found = self._db.execute(
            select(Escrow).where(Escrow.transaction_id == transaction_id)
        ).scalar_one_or_none()
        if found is not None:
            self._escrow_cache[transaction_id] = found
        return found

    def _resolve_destination_phone(self, user_id: uuid.UUID) -> str:
        user = self._db.get(User, user_id)
        if user is None:
            raise EscrowServiceError("Payout destination user was not found.")

        destination = (user.mpesa_phone or user.phone or "").strip()
        if not destination:
            raise EscrowServiceError("Payout destination phone is missing.")

        return destination

    def _default_payout_rail_name(self) -> str:
        try:
            selected, _ = select_payout_rail_for_routing(self._db, self._registry)
            return selected
        except RuntimeError as exc:
            raise EscrowServiceError("No payout rail is currently routable.") from exc

    def _is_split_capability_supported(self) -> bool:
        if self._default_payout_rail_name() == "simulated":
            return True

        capabilities = self._registry.provider.capabilities()
        return capabilities.supports_split_payout or capabilities.supports_partial_release

    @staticmethod
    def _normalize_amount(value: Decimal) -> Decimal:
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise EscrowServiceError("Amount must be a valid decimal value.") from exc

        if amount <= Decimal("0.00"):
            raise EscrowServiceError("Amount must be greater than zero.")

        return amount

    @staticmethod
    def _normalize_ratio(value: Decimal) -> Decimal:
        try:
            ratio = Decimal(str(value)).quantize(Decimal("0.0001"))
        except (InvalidOperation, ValueError) as exc:
            raise EscrowServiceError("Split ratio must be a valid decimal value.") from exc

        if ratio <= Decimal("0.0000") or ratio >= Decimal("1.0000"):
            raise EscrowServiceError("Split ratio must be greater than 0 and less than 1.")

        return ratio


def run_payout_executor_once(
    db: Session,
    *,
    batch_size: int = 25,
    now: datetime | None = None,
) -> PayoutExecutorRunResult:
    service = EscrowService(db)
    if not service.outbox_ready() or batch_size <= 0:
        return PayoutExecutorRunResult(0, 0, 0, 0, 0)

    current_time = now or datetime.now(UTC)
    claimed = list(
        db.execute(
            select(PayoutAttempt)
            .where(PayoutAttempt.outcome == AttemptOutcome.UNKNOWN)
            .order_by(PayoutAttempt.created_at.asc(), PayoutAttempt.attempted_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    if not claimed:
        return PayoutExecutorRunResult(0, 0, 0, 0, 0)

    processed = 0
    succeeded = 0
    failed_definite = 0
    unknown = 0

    for attempt in claimed:
        processed += 1

        escrow = db.get(Escrow, attempt.escrow_id)
        if escrow is None:
            attempt.outcome = AttemptOutcome.FAILED_DEFINITE
            attempt.failure_code = "escrow_missing"
            attempt.failure_reason = "Escrow for payout attempt was not found."
            attempt.attempted_at = current_time
            db.add(attempt)
            failed_definite += 1
            continue

        transaction = db.get(Transaction, escrow.transaction_id)
        if transaction is None:
            attempt.outcome = AttemptOutcome.FAILED_DEFINITE
            attempt.failure_code = "transaction_missing"
            attempt.failure_reason = "Transaction for payout attempt was not found."
            attempt.attempted_at = current_time
            db.add(attempt)
            failed_definite += 1
            continue

        payout_kind = str(attempt.request_snapshot.get("payout_kind") or "release").strip().lower()
        destination_phone = str(attempt.request_snapshot.get("destination_phone") or "").strip()
        if not destination_phone:
            destination_phone = _resolve_destination_phone_for_transaction(
                db,
                transaction=transaction,
                payout_kind=payout_kind,
            )

        try:
            rail = service.registry.get_payout_rail(attempt.rail_name)
        except RuntimeError as exc:
            attempt.outcome = AttemptOutcome.UNKNOWN
            attempt.failure_code = "rail_unavailable"
            attempt.failure_reason = str(exc)
            attempt.response_snapshot = {
                **attempt.response_snapshot,
                "message": str(exc),
            }
            attempt.attempted_at = current_time
            db.add(attempt)
            unknown += 1
            service.refresh_transaction_payout_status(transaction)
            db.add(transaction)
            continue

        if attempt.provider_reference:
            result = _resolve_payout_status_with_inquiry(
                rail,
                attempt=attempt,
            )
        else:
            try:
                result = rail.request_payout(
                    PayoutRequest(
                        escrow_reference=str(escrow.id),
                        amount=attempt.amount,
                        destination_phone=destination_phone,
                        purpose=attempt.purpose,
                        currency=attempt.currency,
                    )
                )
            except (RuntimeError, ValueError, TypeError, LookupError) as exc:
                attempt.outcome = AttemptOutcome.UNKNOWN
                attempt.failure_code = "rail_request_error"
                attempt.failure_reason = str(exc)
                attempt.response_snapshot = {
                    **attempt.response_snapshot,
                    "message": str(exc),
                }
                attempt.attempted_at = current_time
                db.add(attempt)
                record_rail_failure(
                    db,
                    rail_name=attempt.rail_name,
                    provider_name=attempt.provider_name,
                    error_code=attempt.failure_code,
                    error_message=attempt.failure_reason,
                    now=current_time,
                )
                unknown += 1
                service.refresh_transaction_payout_status(transaction)
                db.add(transaction)
                continue

        attempt.provider_reference = result.provider_reference or attempt.provider_reference
        attempt.response_snapshot = result.to_payload()
        attempt.outcome = _attempt_outcome_from_payout(result.outcome)
        attempt.failure_code = _failure_code(attempt.outcome, result.raw_status)
        attempt.failure_reason = _failure_reason(attempt.outcome, result.message)
        attempt.attempted_at = current_time
        db.add(attempt)

        rail_level_outcome = attempt.outcome
        if rail_level_outcome == AttemptOutcome.SUCCEEDED:
            record_rail_success(
                db,
                rail_name=attempt.rail_name,
                provider_name=attempt.provider_name,
                now=current_time,
            )
        else:
            record_rail_failure(
                db,
                rail_name=attempt.rail_name,
                provider_name=attempt.provider_name,
                error_code=attempt.failure_code,
                error_message=attempt.failure_reason,
                now=current_time,
            )

        if attempt.outcome == AttemptOutcome.SUCCEEDED:
            available_balance = _quantize(
                escrow.funded_amount - escrow.released_amount - escrow.refunded_amount
            )
            if available_balance < attempt.amount:
                attempt.outcome = AttemptOutcome.UNKNOWN
                attempt.failure_code = "insufficient_funded_balance"
                attempt.failure_reason = (
                    "Escrow funded balance is insufficient for payout settlement."
                )
                attempt.response_snapshot = {
                    **attempt.response_snapshot,
                    "message": attempt.failure_reason,
                }
                db.add(attempt)
                unknown += 1
                service.refresh_transaction_payout_status(transaction)
                db.add(transaction)
                continue

            movement = LedgerMovement(
                escrow_id=escrow.id,
                idempotency_key=f"outbox:{attempt.id}",
                amount=attempt.amount,
                debit_account_code=(
                    "SELLER_PAYABLE" if payout_kind == "release" else "BUYER_REFUNDABLE"
                ),
                credit_account_code=ESCROW_HELD_ACCOUNT,
                description=f"Payout executor {payout_kind} for {attempt.purpose}",
                currency=attempt.currency,
            )
            post_result = EscrowLedger(db).post_movement(movement)
            if post_result.created_entries == 2:
                if payout_kind == "release":
                    escrow.released_amount = _quantize(escrow.released_amount + attempt.amount)
                else:
                    escrow.refunded_amount = _quantize(escrow.refunded_amount + attempt.amount)
                db.add(escrow)
            succeeded += 1
        elif attempt.outcome == AttemptOutcome.FAILED_DEFINITE:
            failed_definite += 1
        else:
            unknown += 1

        service.refresh_transaction_payout_status(transaction)
        db.add(transaction)

    db.commit()
    return PayoutExecutorRunResult(
        claimed=len(claimed),
        processed=processed,
        succeeded=succeeded,
        failed_definite=failed_definite,
        unknown=unknown,
    )


def run_payout_reconciliation(
    db: Session,
    *,
    unknown_age_minutes: int = 30,
    now: datetime | None = None,
) -> PayoutReconciliationResult:
    service = EscrowService(db)
    if not service.outbox_ready():
        return PayoutReconciliationResult(unknown_alerts=0, mismatch_alerts=0)

    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(minutes=max(1, unknown_age_minutes))
    unknown_alerts = 0
    mismatch_alerts = 0

    unknown_attempts = list(
        db.execute(
            select(PayoutAttempt)
            .where(
                PayoutAttempt.outcome == AttemptOutcome.UNKNOWN,
                PayoutAttempt.provider_reference.is_not(None),
                PayoutAttempt.attempted_at <= cutoff,
            )
            .order_by(PayoutAttempt.attempted_at.asc())
        )
        .scalars()
        .all()
    )

    admin_user_ids = list(
        db.execute(
            select(User.id).where(
                User.role == UserRole.ADMIN,
                User.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    for attempt in unknown_attempts:
        escrow = db.get(Escrow, attempt.escrow_id)
        if escrow is None:
            continue

        transaction = db.get(Transaction, escrow.transaction_id)
        if transaction is None:
            continue

        alert_key = f"payout-unknown:{attempt.id}"
        if _alert_exists(db, transaction_id=transaction.id, alert_key=alert_key):
            continue

        for admin_user_id in admin_user_ids:
            db.add(
                Notification(
                    user_id=admin_user_id,
                    transaction_id=transaction.id,
                    event_type=NotificationEventType.SYSTEM_TIMEOUT,
                    title="Payout reconciliation required",
                    message="Payout attempt remains UNKNOWN and requires manual follow-up.",
                    payload={
                        "alert_key": alert_key,
                        "attempt_id": str(attempt.id),
                        "purpose": attempt.purpose,
                        "provider_reference": attempt.provider_reference,
                    },
                )
            )
        unknown_alerts += 1

    payout_required_statuses = {
        TransactionStatus.RELEASED,
        TransactionStatus.REFUNDED_BUYER,
        TransactionStatus.RESOLVED_RELEASE,
        TransactionStatus.RESOLVED_REFUND,
        TransactionStatus.RESOLVED_SPLIT,
    }
    mismatched_transactions = list(
        db.execute(
            select(Transaction).where(
                Transaction.status.in_(payout_required_statuses),
                Transaction.payout_status == TransactionPayoutStatus.NOT_REQUIRED,
            )
        )
        .scalars()
        .all()
    )

    for transaction in mismatched_transactions:
        alert_key = f"payout-mismatch:{transaction.id}:{transaction.status.value}"
        if _alert_exists(db, transaction_id=transaction.id, alert_key=alert_key):
            continue

        for admin_user_id in admin_user_ids:
            db.add(
                Notification(
                    user_id=admin_user_id,
                    transaction_id=transaction.id,
                    event_type=NotificationEventType.SYSTEM_TIMEOUT,
                    title="Payout status mismatch",
                    message="Transaction is terminal but payout_status is not synchronized.",
                    payload={
                        "alert_key": alert_key,
                        "transaction_status": transaction.status.value,
                        "payout_status": transaction.payout_status.value,
                    },
                )
            )
        mismatch_alerts += 1

    if unknown_alerts or mismatch_alerts:
        db.commit()

    return PayoutReconciliationResult(
        unknown_alerts=unknown_alerts,
        mismatch_alerts=mismatch_alerts,
    )


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


def _alert_exists(db: Session, *, transaction_id: uuid.UUID, alert_key: str) -> bool:
    existing = list(
        db.execute(
            select(Notification).where(
                Notification.transaction_id == transaction_id,
                Notification.event_type == NotificationEventType.SYSTEM_TIMEOUT,
            )
        )
        .scalars()
        .all()
    )
    return any(str(item.payload.get("alert_key", "")) == alert_key for item in existing)


def _quantize(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _resolve_payout_status_with_inquiry(
    rail,
    *,
    attempt: PayoutAttempt,
):
    provider_reference = attempt.provider_reference or ""
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

    result = rail.get_payout_status(provider_reference)
    return result


def _scenario_from_provider_reference(provider_reference: str) -> str:
    parts = provider_reference.split(":")
    if len(parts) >= 4 and parts[0] == "sim":
        return parts[2].strip().lower()
    return ""


def _resolve_destination_phone_for_transaction(
    db: Session,
    *,
    transaction: Transaction,
    payout_kind: str,
) -> str:
    user_id = transaction.seller_id if payout_kind == "release" else transaction.buyer_id
    user = db.get(User, user_id)
    if user is None:
        raise EscrowServiceError("Payout destination user was not found.")

    destination = (user.mpesa_phone or user.phone or "").strip()
    if not destination:
        raise EscrowServiceError("Payout destination phone is missing.")
    return destination
