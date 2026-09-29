import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ledger_entry import LedgerEntry, LedgerEntrySide

ESCROW_HELD_ACCOUNT = "ESCROW_HELD"


class EscrowLedgerError(Exception):
    pass


class EscrowLedgerValidationError(EscrowLedgerError):
    pass


class EscrowLedgerIdempotencyConflictError(EscrowLedgerError):
    pass


class EscrowLedgerUnbalancedError(EscrowLedgerError):
    pass


@dataclass(frozen=True)
class LedgerMovement:
    escrow_id: uuid.UUID
    idempotency_key: str
    amount: Decimal
    debit_account_code: str
    credit_account_code: str
    description: str | None = None
    currency: str = "KES"
    entry_group_id: uuid.UUID | None = None


@dataclass(frozen=True)
class LedgerPostResult:
    escrow_id: uuid.UUID
    entry_group_id: uuid.UUID
    idempotency_key: str
    created_entries: int
    was_idempotent_replay: bool


class EscrowLedger:
    def __init__(self, db: Session) -> None:
        self._db = db

    def post_movement(self, movement: LedgerMovement) -> LedgerPostResult:
        idempotency_key = movement.idempotency_key.strip()
        if not idempotency_key:
            raise EscrowLedgerValidationError("idempotency_key is required.")

        amount = self._normalize_amount(movement.amount)
        debit_account_code = movement.debit_account_code.strip()
        credit_account_code = movement.credit_account_code.strip()

        if not debit_account_code or not credit_account_code:
            raise EscrowLedgerValidationError("Both debit and credit account codes are required.")

        if debit_account_code == credit_account_code:
            raise EscrowLedgerValidationError("Debit and credit account codes must differ.")

        currency = movement.currency.strip().upper()
        if len(currency) != 3:
            raise EscrowLedgerValidationError("Currency must be a three-letter code.")

        existing_entries = self._entries_by_idempotency_key(idempotency_key)
        if existing_entries:
            if any(entry.escrow_id != movement.escrow_id for entry in existing_entries):
                raise EscrowLedgerIdempotencyConflictError(
                    "idempotency_key already used for a different escrow."
                )
            if not self._is_balanced(existing_entries):
                raise EscrowLedgerUnbalancedError(
                    "Existing entries for idempotency_key are not balanced."
                )
            return LedgerPostResult(
                escrow_id=movement.escrow_id,
                entry_group_id=existing_entries[0].entry_group_id,
                idempotency_key=idempotency_key,
                created_entries=0,
                was_idempotent_replay=True,
            )

        entry_group_id = movement.entry_group_id or uuid.uuid4()
        timestamp = datetime.now(UTC)

        debit_entry = LedgerEntry(
            escrow_id=movement.escrow_id,
            entry_group_id=entry_group_id,
            idempotency_key=idempotency_key,
            account_code=debit_account_code,
            side=LedgerEntrySide.DEBIT,
            amount=amount,
            currency=currency,
            description=movement.description,
            created_at=timestamp,
        )
        credit_entry = LedgerEntry(
            escrow_id=movement.escrow_id,
            entry_group_id=entry_group_id,
            idempotency_key=idempotency_key,
            account_code=credit_account_code,
            side=LedgerEntrySide.CREDIT,
            amount=amount,
            currency=currency,
            description=movement.description,
            created_at=timestamp,
        )

        if not self._is_balanced([debit_entry, credit_entry]):
            raise EscrowLedgerUnbalancedError("Ledger movement must be balanced.")

        self._db.add(debit_entry)
        self._db.add(credit_entry)
        self._db.commit()

        return LedgerPostResult(
            escrow_id=movement.escrow_id,
            entry_group_id=entry_group_id,
            idempotency_key=idempotency_key,
            created_entries=2,
            was_idempotent_replay=False,
        )

    def get_escrow_held_balance(self, escrow_id: uuid.UUID) -> Decimal:
        entries = list(
            self._db.execute(
                select(LedgerEntry).where(
                    LedgerEntry.escrow_id == escrow_id,
                    LedgerEntry.account_code == ESCROW_HELD_ACCOUNT,
                )
            )
            .scalars()
            .all()
        )

        balance = Decimal("0.00")
        for entry in entries:
            if entry.side == LedgerEntrySide.DEBIT:
                balance += entry.amount
            elif entry.side == LedgerEntrySide.CREDIT:
                balance -= entry.amount

        return balance.quantize(Decimal("0.01"))

    def _entries_by_idempotency_key(self, idempotency_key: str) -> list[LedgerEntry]:
        return list(
            self._db.execute(
                select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _normalize_amount(value: Decimal) -> Decimal:
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise EscrowLedgerValidationError("Movement amount must be a valid decimal.") from exc

        if amount <= Decimal("0.00"):
            raise EscrowLedgerValidationError("Movement amount must be positive.")

        return amount

    @staticmethod
    def _is_balanced(entries: list[LedgerEntry]) -> bool:
        debit_total = Decimal("0.00")
        credit_total = Decimal("0.00")

        for entry in entries:
            if entry.side == LedgerEntrySide.DEBIT:
                debit_total += entry.amount
            elif entry.side == LedgerEntrySide.CREDIT:
                credit_total += entry.amount

        return debit_total == credit_total
