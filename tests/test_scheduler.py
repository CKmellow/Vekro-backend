"""#273: one scheduler process for every periodic job, and DEMO_FAST_TIMEOUTS."""

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.jobs.scheduler import JOBS, Job, due_jobs, lock_key, run_job
from app.services.deadlines import DEMO_WINDOWS, NORMAL_WINDOWS, timeout_windows
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tests.test_anchoring_batcher import _settings


def _config(**overrides):
    base = {
        "anchor_enabled": False,
        "daraja_enabled": False,
        "econfirm_enabled": False,
        "econfirm_api_key": "",
        "econfirm_code_encryption_key": "",
        "pesapal_enabled": False,
        "pesapal_base_url": "",
        "pesapal_consumer_key": "",
        "pesapal_consumer_secret": "",
        "pesapal_ipn_id": "",
        "frontend_url": "",
    }
    return SimpleNamespace(**(base | overrides))


def test_only_configured_jobs_run_and_each_on_its_interval() -> None:
    names = [job.name for job in due_jobs({}, _config(), now=1000.0)]
    assert names == ["payouts", "timeouts", "liabilities_snapshot", "reserves_reconcile"]

    with_rails = _config(anchor_enabled=True, daraja_enabled=True)
    assert {"anchoring", "confirm_mpesa"} <= {j.name for j in due_jobs({}, with_rails, 0.0)}

    last = {job.name: 1000.0 for job in JOBS}
    assert [j.name for j in due_jobs(last, _config(), now=1059.0)] == ["payouts"]
    assert "timeouts" in [j.name for j in due_jobs(last, _config(), now=1060.0)]


def test_a_failing_job_is_logged_and_never_stops_the_scheduler(caplog) -> None:
    calls: list[str] = []

    def boom(db, settings):
        calls.append("boom")
        raise RuntimeError("provider down")

    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine, class_=Session)
    assert run_job(Job("boom", 1, lambda s: True, boom), _config(), factory) == "failed"
    assert run_job(Job("ok", 1, lambda s: True, lambda db, s: None), _config(), factory) == "ran"
    assert calls == ["boom"] and "scheduler_job_failed job=boom" in caplog.text


def test_lock_keys_are_distinct_signed_64_bit_values() -> None:
    keys = {lock_key(job.name) for job in JOBS}
    assert len(keys) == len(JOBS)
    assert all(-(2**63) <= key < 2**63 for key in keys)


@pytest.mark.postgres
@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="Needs TEST_POSTGRES_URL.")
def test_two_instances_never_run_the_same_job_at_once() -> None:
    engine = create_engine(
        os.environ["TEST_POSTGRES_URL"].replace("postgresql://", "postgresql+psycopg://")
    )
    factory = sessionmaker(bind=engine, class_=Session)
    ran: list[str] = []
    job = Job("payouts", 30, lambda s: True, lambda db, s: ran.append("ran"))

    with engine.connect() as other_instance:
        other_instance.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key("payouts")})
        assert run_job(job, _config(), factory) == "skipped_locked"
        other_instance.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key("payouts")})
    assert run_job(job, _config(), factory) == "ran"
    assert ran == ["ran"]


def test_demo_fast_timeouts_shrink_the_windows_to_minutes() -> None:
    assert timeout_windows(SimpleNamespace(demo_fast_timeouts=False)) == NORMAL_WINDOWS
    fast = timeout_windows(SimpleNamespace(demo_fast_timeouts=True))
    assert fast == DEMO_WINDOWS
    assert fast.hold == timedelta(minutes=3) and NORMAL_WINDOWS.hold == timedelta(hours=24)
    assert all(
        getattr(fast, name) < getattr(NORMAL_WINDOWS, name)
        for name in ("at_door", "no_dispatch", "hold", "return_send", "return_receipt")
    )


def test_demo_fast_timeouts_are_refused_in_production() -> None:
    with pytest.raises(ValueError, match="DEMO_FAST_TIMEOUTS"):
        _settings(environment="production", demo_fast_timeouts=True)


def test_the_hold_auto_release_follows_the_demo_window(monkeypatch) -> None:
    """With DEMO_FAST_TIMEOUTS the 24h hold releases after minutes, through the same job."""
    from datetime import UTC, datetime

    from app.models.transaction import TransactionStatus
    from app.services import transaction as transaction_service
    from app.services.transaction import run_timeout_jobs

    from tests.test_ussd import _order, _with_tables

    db = _with_tables()
    buyer, _seller, order = _order(db, monkeypatch, "40", serialized=True, until="door")
    transaction_service.confirm_buyer_delivery_otp(db, order.id, buyer, otp_code="246810")
    db.refresh(order)
    assert order.status == TransactionStatus.HOLD_24H

    monkeypatch.setattr(
        "app.services.deadlines.timeout_windows", lambda settings=None: DEMO_WINDOWS
    )
    monkeypatch.setattr(transaction_service, "timeout_windows", lambda settings=None: DEMO_WINDOWS)
    run_timeout_jobs(db, now=datetime.now(UTC) + timedelta(minutes=4))
    db.refresh(order)
    assert order.status != TransactionStatus.HOLD_24H
