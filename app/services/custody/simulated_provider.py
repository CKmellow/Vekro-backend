import uuid
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection_attempt import AttemptOutcome, CollectionAttempt
from app.models.escrow import Escrow
from app.models.payout_attempt import PayoutAttempt
from app.services.custody.dto import (
    CollectionResult,
    CustodyCapabilities,
    EscrowRecord,
    EscrowStatusResult,
    FundingRequest,
    OpenEscrowRequest,
    PayoutRequest,
    PayoutResult,
)
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.ports import CollectionRail, PayoutRail
from app.services.custody.simulated_rail import SimulatedRail
from app.services.ledger import ESCROW_HELD_ACCOUNT, EscrowLedger, LedgerMovement

SIMULATED_PROVIDER_NAME = "simulated"
SIMULATED_ESCROW_REFERENCE_PREFIX = "sim-escrow-"


class SimulatedCustodyProviderError(Exception):
    pass


class SimulatedEscrowReferenceError(SimulatedCustodyProviderError):
    pass


class SimulatedEscrowNotFoundError(SimulatedCustodyProviderError):
    pass


class SimulatedCustodyProvider:
    def __init__(
        self,
        db: Session,
        *,
        collection_rail: CollectionRail | None = None,
        payout_rail: PayoutRail | None = None,
        ledger: EscrowLedger | None = None,
    ) -> None:
        shared_rail = SimulatedRail()
        self._db = db
        self._collection_rail = collection_rail or shared_rail
        self._payout_rail = payout_rail or shared_rail
        self._ledger = ledger or EscrowLedger(db)

    def capabilities(self) -> CustodyCapabilities:
        return CustodyCapabilities(
            holds_funds_structurally=False,
            supports_split_payout=False,
            supports_partial_release=True,
            supports_webhook_auth=False,
        )

    def open_escrow(self, request: OpenEscrowRequest) -> EscrowRecord:
        transaction_id = self._parse_uuid(request.transaction_id, field_name="transaction_id")
        buyer_id = self._parse_uuid(request.buyer_id, field_name="buyer_id")
        seller_id = self._parse_uuid(request.seller_id, field_name="seller_id")

        existing = self._db.execute(
            select(Escrow).where(Escrow.transaction_id == transaction_id)
        ).scalar_one_or_none()
        if existing is not None:
            return self._to_escrow_record(existing)

        currency = self._normalize_currency(request.currency)
        escrow = Escrow(
            transaction_id=transaction_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount_total=self._normalize_amount(request.amount),
            currency=currency,
        )
        self._db.add(escrow)
        self._db.commit()
        self._db.refresh(escrow)

        return self._to_escrow_record(escrow)

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        escrow = self._get_escrow_from_reference(request.escrow_reference)
        result = self._collection_rail.request_funding(request)
        attempt_outcome = self._attempt_outcome_from_collection(result.outcome)
        amount = self._normalize_amount(request.amount)
        currency = self._normalize_currency(request.currency)

        collection_attempt = CollectionAttempt(
            escrow_id=escrow.id,
            rail_name=SIMULATED_PROVIDER_NAME,
            provider_name=SIMULATED_PROVIDER_NAME,
            idempotency_key=self._funding_idempotency_key(result),
            provider_reference=result.provider_reference,
            amount=amount,
            currency=currency,
            outcome=attempt_outcome,
            request_snapshot=request.to_payload(),
            response_snapshot=result.to_payload(),
            failure_code=self._failure_code(attempt_outcome, result.raw_status),
            failure_reason=self._failure_reason(attempt_outcome, result.message),
        )
        self._db.add(collection_attempt)

        duplicate_compensation = False

        if result.outcome == CollectionOutcome.SUCCEEDED:
            post_result = self._ledger.post_movement(
                LedgerMovement(
                    escrow_id=escrow.id,
                    idempotency_key=self._funding_idempotency_key(result),
                    amount=amount,
                    debit_account_code=ESCROW_HELD_ACCOUNT,
                    credit_account_code="BUYER_CLEARING",
                    description="Simulated custody funding",
                    currency=currency,
                )
            )
            if post_result.created_entries == 2:
                escrow.funded_amount = self._quantize(escrow.funded_amount + request.amount)

            duplicate_compensation = self._queue_duplicate_compensating_refund(
                escrow=escrow,
                amount=amount,
                currency=currency,
                provider_reference=result.provider_reference,
                raw_status=result.raw_status,
            )

        if duplicate_compensation:
            metadata = {
                "compensating_refund_queued": True,
                "admin_flag_required": True,
            }
            current_payload = dict(collection_attempt.response_snapshot)
            existing_metadata = current_payload.get("metadata")
            merged_metadata = (
                {**existing_metadata, **metadata}
                if isinstance(existing_metadata, dict)
                else metadata
            )
            collection_attempt.response_snapshot = {
                **current_payload,
                "metadata": merged_metadata,
            }
            self._db.add(collection_attempt)

        self._db.commit()
        return result

    def get_status(self, escrow_reference: str) -> EscrowStatusResult:
        escrow = self._get_escrow_from_reference(escrow_reference)
        return EscrowStatusResult(
            escrow_reference=self._format_escrow_reference(escrow.id),
            funded_amount=escrow.funded_amount,
            released_amount=escrow.released_amount,
            refunded_amount=escrow.refunded_amount,
        )

    def release(self, request: PayoutRequest) -> PayoutResult:
        return self._request_payout(
            request,
            payout_type="release",
            debit_account_code="SELLER_PAYABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )

    def refund(self, request: PayoutRequest) -> PayoutResult:
        return self._request_payout(
            request,
            payout_type="refund",
            debit_account_code="BUYER_REFUNDABLE",
            credit_account_code=ESCROW_HELD_ACCOUNT,
        )

    def _request_payout(
        self,
        request: PayoutRequest,
        *,
        payout_type: str,
        debit_account_code: str,
        credit_account_code: str,
    ) -> PayoutResult:
        escrow = self._get_escrow_from_reference(request.escrow_reference)
        result = self._payout_rail.request_payout(request)
        attempt_outcome = self._attempt_outcome_from_payout(result.outcome)

        attempt = self._existing_open_payout_attempt(
            escrow_id=escrow.id,
            purpose=request.purpose,
            outcome=attempt_outcome,
        )

        if attempt is None:
            attempt = PayoutAttempt(
                escrow_id=escrow.id,
                purpose=request.purpose,
                rail_name=SIMULATED_PROVIDER_NAME,
                provider_name=SIMULATED_PROVIDER_NAME,
                idempotency_key=self._payout_idempotency_key(payout_type, result),
                provider_reference=result.provider_reference,
                amount=self._normalize_amount(request.amount),
                currency=self._normalize_currency(request.currency),
                outcome=attempt_outcome,
                request_snapshot=request.to_payload(),
                response_snapshot=result.to_payload(),
                failure_code=self._failure_code(attempt_outcome, result.raw_status),
                failure_reason=self._failure_reason(attempt_outcome, result.message),
            )
            self._db.add(attempt)
        else:
            attempt.idempotency_key = self._payout_idempotency_key(payout_type, result)
            attempt.provider_reference = result.provider_reference
            attempt.amount = self._normalize_amount(request.amount)
            attempt.currency = self._normalize_currency(request.currency)
            attempt.outcome = attempt_outcome
            attempt.request_snapshot = request.to_payload()
            attempt.response_snapshot = result.to_payload()
            attempt.failure_code = self._failure_code(attempt_outcome, result.raw_status)
            attempt.failure_reason = self._failure_reason(attempt_outcome, result.message)

        if result.outcome == PayoutOutcome.SUCCEEDED:
            post_result = self._ledger.post_movement(
                LedgerMovement(
                    escrow_id=escrow.id,
                    idempotency_key=self._payout_idempotency_key(payout_type, result),
                    amount=self._normalize_amount(request.amount),
                    debit_account_code=debit_account_code,
                    credit_account_code=credit_account_code,
                    description=f"Simulated custody {payout_type}",
                    currency=self._normalize_currency(request.currency),
                )
            )

            if post_result.created_entries == 2:
                if payout_type == "release":
                    escrow.released_amount = self._quantize(escrow.released_amount + request.amount)
                else:
                    escrow.refunded_amount = self._quantize(escrow.refunded_amount + request.amount)

        self._db.commit()
        return result

    def _existing_open_payout_attempt(
        self,
        *,
        escrow_id: uuid.UUID,
        purpose: str,
        outcome: AttemptOutcome,
    ) -> PayoutAttempt | None:
        if outcome == AttemptOutcome.FAILED_DEFINITE:
            return None

        return self._db.execute(
            select(PayoutAttempt).where(
                PayoutAttempt.escrow_id == escrow_id,
                PayoutAttempt.purpose == purpose,
                PayoutAttempt.outcome != AttemptOutcome.FAILED_DEFINITE,
            )
        ).scalar_one_or_none()

    def _get_escrow_from_reference(self, escrow_reference: str) -> Escrow:
        escrow_id = self._parse_escrow_reference(escrow_reference)
        escrow = self._db.get(Escrow, escrow_id)
        if escrow is None:
            raise SimulatedEscrowNotFoundError(
                f"Escrow reference '{escrow_reference}' was not found."
            )
        return escrow

    @staticmethod
    def _attempt_outcome_from_collection(outcome: CollectionOutcome) -> AttemptOutcome:
        if outcome == CollectionOutcome.SUCCEEDED:
            return AttemptOutcome.SUCCEEDED
        if outcome == CollectionOutcome.FAILED_DEFINITE:
            return AttemptOutcome.FAILED_DEFINITE
        return AttemptOutcome.UNKNOWN

    @staticmethod
    def _attempt_outcome_from_payout(outcome: PayoutOutcome) -> AttemptOutcome:
        if outcome == PayoutOutcome.SUCCEEDED:
            return AttemptOutcome.SUCCEEDED
        if outcome == PayoutOutcome.FAILED_DEFINITE:
            return AttemptOutcome.FAILED_DEFINITE
        return AttemptOutcome.UNKNOWN

    @staticmethod
    def _failure_code(outcome: AttemptOutcome, raw_status: str | None) -> str | None:
        if outcome == AttemptOutcome.SUCCEEDED:
            return None
        return raw_status or outcome.value

    @staticmethod
    def _failure_reason(outcome: AttemptOutcome, message: str | None) -> str | None:
        if outcome == AttemptOutcome.SUCCEEDED:
            return None
        return message

    @staticmethod
    def _funding_idempotency_key(result: CollectionResult) -> str:
        return f"sim-funding:{result.provider_reference}"

    @staticmethod
    def _payout_idempotency_key(payout_type: str, result: PayoutResult) -> str:
        return f"sim-{payout_type}:{result.provider_reference}"

    def _queue_duplicate_compensating_refund(
        self,
        *,
        escrow: Escrow,
        amount: Decimal,
        currency: str,
        provider_reference: str,
        raw_status: str | None,
    ) -> bool:
        status = str(raw_status or "").strip().lower()
        if status not in {"duplicate", "duplicate_confirmed"}:
            return False

        purpose = self._duplicate_refund_purpose(provider_reference)
        existing = self._db.execute(
            select(PayoutAttempt).where(
                PayoutAttempt.escrow_id == escrow.id,
                PayoutAttempt.purpose == purpose,
                PayoutAttempt.outcome != AttemptOutcome.FAILED_DEFINITE,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return True

        self._db.add(
            PayoutAttempt(
                escrow_id=escrow.id,
                purpose=purpose,
                rail_name=SIMULATED_PROVIDER_NAME,
                provider_name=SIMULATED_PROVIDER_NAME,
                idempotency_key=f"sim-dup-refund:{provider_reference}",
                provider_reference=f"sim-compensate-{uuid5(NAMESPACE_URL, provider_reference).hex[:16]}",
                amount=amount,
                currency=currency,
                outcome=AttemptOutcome.UNKNOWN,
                request_snapshot={
                    "escrow_reference": self._format_escrow_reference(escrow.id),
                    "amount": str(amount),
                    "currency": currency,
                    "duplicate_provider_reference": provider_reference,
                    "reason": "duplicate_confirmed_collection",
                },
                response_snapshot={
                    "state": "queued_compensating_refund",
                    "admin_flag": "duplicate_funding_auto_refund",
                },
                failure_code="compensating_refund_required",
                failure_reason=(
                    "Duplicate confirmed collection requires compensating refund and admin review."
                ),
            )
        )
        return True

    @staticmethod
    def _duplicate_refund_purpose(provider_reference: str) -> str:
        suffix = uuid5(NAMESPACE_URL, provider_reference).hex[:20]
        return f"dup-refund:{suffix}"

    @staticmethod
    def _format_escrow_reference(escrow_id: uuid.UUID) -> str:
        return f"{SIMULATED_ESCROW_REFERENCE_PREFIX}{escrow_id}"

    @staticmethod
    def _parse_uuid(value: str, *, field_name: str) -> uuid.UUID:
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise SimulatedEscrowReferenceError(
                f"Invalid UUID provided for {field_name}: '{value}'."
            ) from exc

    @classmethod
    def _parse_escrow_reference(cls, reference: str) -> uuid.UUID:
        candidate = reference
        if reference.startswith(SIMULATED_ESCROW_REFERENCE_PREFIX):
            candidate = reference[len(SIMULATED_ESCROW_REFERENCE_PREFIX) :]
        return cls._parse_uuid(candidate, field_name="escrow_reference")

    @staticmethod
    def _normalize_currency(value: str) -> str:
        currency = value.strip().upper()
        if len(currency) != 3:
            raise SimulatedEscrowReferenceError("Currency must be a three-letter code.")
        return currency

    @staticmethod
    def _normalize_amount(value: Decimal) -> Decimal:
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise SimulatedEscrowReferenceError("Amount must be a valid decimal value.") from exc

        if amount <= Decimal("0.00"):
            raise SimulatedEscrowReferenceError("Amount must be greater than zero.")

        return amount

    @staticmethod
    def _quantize(value: Decimal) -> Decimal:
        return Decimal(str(value)).quantize(Decimal("0.01"))

    @classmethod
    def _to_escrow_record(cls, escrow: Escrow) -> EscrowRecord:
        return EscrowRecord(
            escrow_reference=cls._format_escrow_reference(escrow.id),
            transaction_id=str(escrow.transaction_id),
            amount=escrow.amount_total,
            currency=escrow.currency,
        )
