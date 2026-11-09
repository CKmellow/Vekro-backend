"""One process that runs every periodic job (M24 B4, #273; docs/demo-modes.md "Scheduler").

    python -m app.jobs.scheduler            # run forever
    python -m app.jobs.scheduler --once     # run every enabled job once, then exit

Each job has its own interval and runs only when its feature is configured. On PostgreSQL every
run holds a session advisory lock named after the job, so two scheduler instances never run the
same job at once (the second skips it). A failing job is logged and retried at its next interval;
it never stops the others.
"""

from __future__ import annotations

import argparse
import logging
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.settings import get_settings

_logger = logging.getLogger("app.jobs.scheduler")


@dataclass(frozen=True)
class Job:
    name: str
    interval_seconds: int
    enabled: Callable[[Any], bool]
    run: Callable[[Session, Any], Any]


def _always(_settings: Any) -> bool:
    return True


def _anchoring(_db: Session, _settings: Any) -> Any:
    from app.jobs.anchoring import _step

    return _step()  # its own session and chain client


def _payouts(db: Session, _settings: Any) -> Any:
    from app.services.escrow_service import run_payout_executor_once, run_payout_reconciliation

    executed = run_payout_executor_once(db)
    reconciled = run_payout_reconciliation(db)
    return executed, reconciled


def _econfirm_usable(settings: Any) -> bool:
    from app.services.payment_rails import _econfirm_usable as usable

    return usable(settings)


def _econfirm_poll(db: Session, settings: Any) -> Any:
    from app.services.custody.econfirm_orchestration import run_econfirm_funding_reconciliation
    from app.services.custody.registry import build_econfirm_provider

    return run_econfirm_funding_reconciliation(
        db, collection_rail=build_econfirm_provider(settings)
    )


def _mpesa(db: Session, settings: Any) -> Any:
    from app.services.daraja import DarajaClient
    from app.services.daraja_confirmation import confirm_pending_payments

    return confirm_pending_payments(db, client=DarajaClient.from_settings(settings))


def _card_enabled(settings: Any) -> bool:
    from app.services.card_payments import card_payments_available

    return card_payments_available(settings)


def _cards(db: Session, settings: Any) -> Any:
    from app.services.card_payments import build_rail, confirm_pending_card_payments

    return confirm_pending_card_payments(db, rail=build_rail(settings))


def _liabilities(db: Session, _settings: Any) -> Any:
    from app.services.anchoring.liabilities import build_liabilities_snapshot

    return build_liabilities_snapshot(db)


def _reserves(db: Session, settings: Any) -> Any:
    from app.services.anchoring.reserves import reconcile_reserves
    from app.services.custody.registry import build_econfirm_provider

    status_of = (
        build_econfirm_provider(settings).get_funding_status if _econfirm_usable(settings) else None
    )
    return reconcile_reserves(db, econfirm_status_of=status_of)


def _timeouts(db: Session, _settings: Any) -> Any:
    from app.services.transaction import run_timeout_jobs

    return run_timeout_jobs(db)


JOBS = (
    Job("anchoring", 30, lambda s: bool(s.anchor_enabled), _anchoring),
    Job("payouts", 30, _always, _payouts),
    Job("econfirm_poll", 60, _econfirm_usable, _econfirm_poll),
    Job("confirm_mpesa", 60, lambda s: bool(s.daraja_enabled), _mpesa),
    Job("confirm_card", 60, _card_enabled, _cards),
    Job("timeouts", 60, _always, _timeouts),
    Job("liabilities_snapshot", 300, _always, _liabilities),
    Job("reserves_reconcile", 900, _always, _reserves),
)


def lock_key(name: str) -> int:
    """A stable signed 64-bit key per job for pg advisory locks."""
    return zlib.crc32(f"vekro-scheduler:{name}".encode()) - 2**31


def _try_lock(db: Session, name: str) -> bool:
    if db.get_bind().dialect.name != "postgresql":
        return True  # single process (SQLite in tests): nothing to coordinate
    return bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(name)}).scalar())


def _unlock(db: Session, name: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(name)})


def run_job(job: Job, settings: Any, session_factory: Callable[[], Session]) -> str:
    """Run one job under its lock. Returns ran, skipped_locked or failed."""
    with session_factory() as db:
        if not _try_lock(db, job.name):
            _logger.info("scheduler_job_skipped_locked job=%s", job.name)
            return "skipped_locked"
        try:
            job.run(db, settings)
            _logger.info("scheduler_job_ran job=%s", job.name)
            return "ran"
        except Exception:
            db.rollback()
            _logger.exception("scheduler_job_failed job=%s", job.name)
            return "failed"
        finally:
            try:
                _unlock(db, job.name)
                db.commit()
            except Exception:  # pragma: no cover - the connection is gone; the lock went with it
                _logger.warning("scheduler_unlock_failed job=%s", job.name)


def due_jobs(last_run: dict[str, float], settings: Any, now: float) -> list[Job]:
    return [
        job
        for job in JOBS
        if job.enabled(settings)
        and now - last_run.get(job.name, float("-inf")) >= job.interval_seconds
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.scheduler")
    parser.add_argument("--once", action="store_true", help="Run every enabled job once.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from app.db.session import _get_session_factory

    settings = get_settings()
    factory = _get_session_factory()
    enabled = [job.name for job in JOBS if job.enabled(settings)]
    _logger.info(
        "scheduler_started jobs=%s demo_fast_timeouts=%s", enabled, settings.demo_fast_timeouts
    )
    last_run: dict[str, float] = {}
    while True:
        now = time.monotonic()
        for job in due_jobs(last_run, settings, now):
            run_job(job, settings, factory)
            last_run[job.name] = now
        if args.once:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
