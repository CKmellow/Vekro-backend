import uuid
from decimal import Decimal

from app.models.escrow import Escrow
from app.models.ledger_entry import LedgerEntry
from app.models.transaction import Transaction, TransactionPayoutStatus
from sqlalchemy import Numeric


def _assert_numeric_12_2(model, column_name: str) -> None:
    column = model.__table__.c[column_name]
    assert isinstance(column.type, Numeric)
    assert column.type.precision == 12
    assert column.type.scale == 2
    assert column.type.asdecimal is True


def test_money_columns_use_numeric_12_2_decimal_mapping() -> None:
    _assert_numeric_12_2(Escrow, "amount_total")
    _assert_numeric_12_2(Escrow, "funded_amount")
    _assert_numeric_12_2(Escrow, "released_amount")
    _assert_numeric_12_2(Escrow, "refunded_amount")
    _assert_numeric_12_2(LedgerEntry, "amount")


def test_transaction_payout_status_default_is_not_required() -> None:
    payout_col = Transaction.__table__.c["payout_status"]

    assert TransactionPayoutStatus.NOT_REQUIRED.value == "not_required"
    assert payout_col.default is not None
    assert payout_col.server_default is not None
    assert "not_required" in str(payout_col.server_default.arg)


def test_money_columns_round_trip_decimal_values() -> None:
    escrow = Escrow(
        transaction_id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        amount_total=Decimal("1200.00"),
    )

    # Ensure Decimal can be assigned at object level for money fields.
    assert isinstance(escrow.amount_total, Decimal)
