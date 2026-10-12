from datetime import UTC, datetime, timedelta

import pytest
from app.models.transaction import TransactionStatus
from app.services import transaction as transaction_service
from app.services.transaction import run_timeout_jobs

from tests.test_escrow_service_orchestration import _build_db_session, _seed_transaction

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_timeout_sweep_uses_documented_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    cutoffs: dict[str, datetime] = {}

    def _record(name: str):
        def _query(_db, cutoff_time):
            cutoffs[name] = cutoff_time
            return []

        return _query

    for name in (
        "_get_due_at_door_timeout_transactions",
        "_get_due_locked_timeout_transactions",
        "_get_due_hold_auto_release_transactions",
        "_get_due_dispute_buyer_sent_back_timeout_transactions",
        "_get_due_seller_received_timeout_transactions",
    ):
        monkeypatch.setattr(transaction_service, name, _record(name))

    run_timeout_jobs(object(), now=NOW)

    assert cutoffs == {
        "_get_due_at_door_timeout_transactions": NOW - timedelta(hours=1),
        "_get_due_locked_timeout_transactions": NOW - timedelta(hours=48),
        "_get_due_hold_auto_release_transactions": NOW - timedelta(hours=24),
        "_get_due_dispute_buyer_sent_back_timeout_transactions": NOW - timedelta(days=3),
        "_get_due_seller_received_timeout_transactions": NOW - timedelta(days=3),
    }


@pytest.mark.parametrize(
    ("status", "timestamp_field", "window", "result_field", "expected_status"),
    [
        (
            TransactionStatus.AT_DOOR_PENDING_INSPECTION,
            "at_door_at",
            timedelta(hours=1),
            "at_door_timeout_count",
            TransactionStatus.REFUNDED_BUYER,
        ),
        (
            TransactionStatus.LOCKED,
            "locked_at",
            timedelta(hours=48),
            "no_dispatch_timeout_count",
            TransactionStatus.REFUNDED_BUYER,
        ),
        (
            TransactionStatus.HOLD_24H,
            "hold_started_at",
            timedelta(hours=24),
            "hold_auto_release_count",
            TransactionStatus.RELEASED,
        ),
    ],
)
@pytest.mark.parametrize("past_window", [False, True])
def test_timeout_fires_only_after_its_window(
    status: TransactionStatus,
    timestamp_field: str,
    window: timedelta,
    result_field: str,
    expected_status: TransactionStatus,
    past_window: bool,
) -> None:
    db = _build_db_session()
    try:
        transaction = _seed_transaction(db, status=status)
        offset = timedelta(seconds=1) if past_window else -timedelta(seconds=1)
        setattr(transaction, timestamp_field, NOW - window - offset)
        db.commit()

        result = run_timeout_jobs(db, now=NOW)
        db.refresh(transaction)

        if past_window:
            assert getattr(result, result_field) == 1
            assert transaction.status == expected_status
        else:
            assert getattr(result, result_field) == 0
            assert transaction.status == status
    finally:
        db.close()
