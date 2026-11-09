"""State timeout windows, shared by the timeout jobs and the deadlines clients read."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models.transaction import Transaction, TransactionStatus

# How long each state may last before run_timeout_jobs acts on it.
AT_DOOR_INSPECTION_WINDOW = timedelta(hours=1)
NO_DISPATCH_WINDOW = timedelta(hours=48)
HOLD_WINDOW = timedelta(hours=24)
RETURN_SEND_WINDOW = timedelta(days=3)
RETURN_RECEIPT_WINDOW = timedelta(days=3)


@dataclass(frozen=True)
class TimeoutWindows:
    at_door: timedelta
    no_dispatch: timedelta
    hold: timedelta
    return_send: timedelta
    return_receipt: timedelta


NORMAL_WINDOWS = TimeoutWindows(
    AT_DOOR_INSPECTION_WINDOW,
    NO_DISPATCH_WINDOW,
    HOLD_WINDOW,
    RETURN_SEND_WINDOW,
    RETURN_RECEIPT_WINDOW,
)
# DEMO_FAST_TIMEOUTS (never production): the same rules, in minutes, for a class demo.
DEMO_WINDOWS = TimeoutWindows(
    at_door=timedelta(minutes=2),
    no_dispatch=timedelta(minutes=5),
    hold=timedelta(minutes=3),
    return_send=timedelta(minutes=3),
    return_receipt=timedelta(minutes=3),
)


def timeout_windows(settings=None) -> TimeoutWindows:
    if settings is None:
        from app.core.settings import get_settings

        settings = get_settings()
    return DEMO_WINDOWS if getattr(settings, "demo_fast_timeouts", False) else NORMAL_WINDOWS


@dataclass(frozen=True)
class TransactionDeadlines:
    """When the current state times out. Only the field for the current state is set."""

    dispatch_by: datetime | None = None
    inspect_by: datetime | None = None
    hold_until: datetime | None = None
    return_send_by: datetime | None = None
    return_confirm_by: datetime | None = None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _after(start: datetime | None, window: timedelta) -> datetime | None:
    start = _utc(start)
    return start + window if start is not None else None


def deadlines_for(transaction: Transaction) -> TransactionDeadlines:
    """Mirrors the cutoffs in run_timeout_jobs, so a deadline is when the job will act."""
    status = transaction.status
    windows = timeout_windows()
    if status == TransactionStatus.LOCKED:
        return TransactionDeadlines(dispatch_by=_after(transaction.locked_at, windows.no_dispatch))
    if status == TransactionStatus.AT_DOOR_PENDING_INSPECTION:
        return TransactionDeadlines(inspect_by=_after(transaction.at_door_at, windows.at_door))
    if status == TransactionStatus.HOLD_24H:
        return TransactionDeadlines(hold_until=_after(transaction.hold_started_at, windows.hold))
    if status == TransactionStatus.DISPUTED_FUNCTIONAL:
        return TransactionDeadlines(
            return_send_by=_after(transaction.updated_at, windows.return_send)
        )
    if status == TransactionStatus.RETURN_IN_TRANSIT:
        return TransactionDeadlines(
            return_confirm_by=_after(transaction.updated_at, windows.return_receipt)
        )
    return TransactionDeadlines()
