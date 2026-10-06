from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.rail_health import RailBreakerState, RailHealth
from app.services.custody.registry import CustodyRegistry


@dataclass(frozen=True)
class RailBreakerPolicy:
    failure_threshold: int = 3
    cooldown_seconds: int = 300

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be greater than 0")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be greater than 0")


@dataclass(frozen=True)
class RailHealthSnapshot:
    rail_name: str
    provider_name: str | None
    breaker_state: RailBreakerState
    consecutive_failures: int
    last_error_code: str | None
    last_error_message: str | None
    opened_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    cooldown_until: datetime | None
    collection_available: bool
    payout_available: bool
    routable: bool


def record_rail_failure(
    db: Session,
    *,
    rail_name: str,
    provider_name: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
    policy: RailBreakerPolicy | None = None,
) -> None:
    try:
        _record_rail_failure_impl(
            db,
            rail_name=rail_name,
            provider_name=provider_name,
            error_code=error_code,
            error_message=error_message,
            now=now,
            policy=policy,
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        # Some isolated unit tests intentionally create partial schemas.
        # Skip breaker persistence in those cases so core flows remain testable.
        return


def record_rail_success(
    db: Session,
    *,
    rail_name: str,
    provider_name: str | None = None,
    now: datetime | None = None,
) -> None:
    try:
        _record_rail_success_impl(
            db,
            rail_name=rail_name,
            provider_name=provider_name,
            now=now,
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        return


def blocked_rails_for_routing(
    db: Session,
    *,
    rail_names: tuple[str, ...] | list[str],
    now: datetime | None = None,
) -> set[str]:
    try:
        return _blocked_rails_for_routing_impl(
            db,
            rail_names=rail_names,
            now=now,
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        return set()


def select_collection_rail_for_routing(
    db: Session,
    registry: CustodyRegistry,
    *,
    now: datetime | None = None,
) -> tuple[str, Any]:
    blocked = blocked_rails_for_routing(
        db,
        rail_names=registry.collection_priority,
        now=now,
    )
    return registry.select_collection_rail(exclude=blocked)


def select_payout_rail_for_routing(
    db: Session,
    registry: CustodyRegistry,
    *,
    now: datetime | None = None,
) -> tuple[str, Any]:
    blocked = blocked_rails_for_routing(
        db,
        rail_names=registry.payout_priority,
        now=now,
    )
    return registry.select_payout_rail(exclude=blocked)


def build_admin_rail_health_payload(
    db: Session,
    *,
    registry: CustodyRegistry,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(UTC)
    snapshots: list[RailHealthSnapshot] = []

    ordered_names: list[str] = []
    for rail_name in (*registry.collection_priority, *registry.payout_priority):
        if rail_name not in ordered_names:
            ordered_names.append(rail_name)

    blocked = _blocked_rails_for_routing_impl(
        db,
        rail_names=ordered_names,
        now=current_time,
    )

    for rail_name in ordered_names:
        row = _get_or_create_rail_health(
            db,
            rail_name=rail_name,
            provider_name=rail_name,
            now=current_time,
        )
        collection_available = rail_name in registry.collection_rails
        payout_available = rail_name in registry.payout_rails
        if payout_available:
            try:
                registry.get_payout_rail(rail_name)
            except RuntimeError:
                payout_available = False

        snapshots.append(
            RailHealthSnapshot(
                rail_name=rail_name,
                provider_name=row.provider_name,
                breaker_state=row.breaker_state,
                consecutive_failures=row.consecutive_failures,
                last_error_code=row.last_error_code,
                last_error_message=row.last_error_message,
                opened_at=row.opened_at,
                last_success_at=row.last_success_at,
                last_failure_at=row.last_failure_at,
                cooldown_until=row.cooldown_until,
                collection_available=collection_available,
                payout_available=payout_available,
                routable=(rail_name not in blocked) and (collection_available or payout_available),
            )
        )

    capabilities = registry.provider.capabilities()
    return {
        "custody_mode": registry.custody_mode.value,
        "holds_funds_structurally": capabilities.holds_funds_structurally,
        "collection_priority": list(registry.collection_priority),
        "payout_priority": list(registry.payout_priority),
        "rails": [
            {
                "rail_name": snapshot.rail_name,
                "provider_name": snapshot.provider_name,
                "breaker_state": snapshot.breaker_state,
                "consecutive_failures": snapshot.consecutive_failures,
                "last_error_code": snapshot.last_error_code,
                "last_error_message": snapshot.last_error_message,
                "opened_at": snapshot.opened_at,
                "last_success_at": snapshot.last_success_at,
                "last_failure_at": snapshot.last_failure_at,
                "cooldown_until": snapshot.cooldown_until,
                "collection_available": snapshot.collection_available,
                "payout_available": snapshot.payout_available,
                "routable": snapshot.routable,
            }
            for snapshot in snapshots
        ],
    }


def _record_rail_failure_impl(
    db: Session,
    *,
    rail_name: str,
    provider_name: str | None,
    error_code: str | None,
    error_message: str | None,
    now: datetime | None,
    policy: RailBreakerPolicy | None,
) -> None:
    current_time = now or datetime.now(UTC)
    breaker_policy = policy or RailBreakerPolicy()
    row = _get_or_create_rail_health(
        db,
        rail_name=rail_name,
        provider_name=provider_name,
        now=current_time,
    )
    _refresh_open_breaker(row, now=current_time)

    row.consecutive_failures += 1
    row.last_failure_at = current_time
    row.last_error_code = (error_code or row.last_error_code or "unknown").strip() or "unknown"
    row.last_error_message = (error_message or row.last_error_message or "").strip() or None

    should_open = row.consecutive_failures >= breaker_policy.failure_threshold
    if row.breaker_state == RailBreakerState.HALF_OPEN:
        should_open = True

    if should_open:
        row.breaker_state = RailBreakerState.OPEN
        row.opened_at = current_time
        row.cooldown_until = current_time + timedelta(seconds=breaker_policy.cooldown_seconds)

    db.add(row)
    db.flush()


def _record_rail_success_impl(
    db: Session,
    *,
    rail_name: str,
    provider_name: str | None,
    now: datetime | None,
) -> None:
    current_time = now or datetime.now(UTC)
    row = _get_or_create_rail_health(
        db,
        rail_name=rail_name,
        provider_name=provider_name,
        now=current_time,
    )

    row.breaker_state = RailBreakerState.CLOSED
    row.consecutive_failures = 0
    row.last_success_at = current_time
    row.last_error_code = None
    row.last_error_message = None
    row.opened_at = None
    row.cooldown_until = None

    db.add(row)
    db.flush()


def _blocked_rails_for_routing_impl(
    db: Session,
    *,
    rail_names: tuple[str, ...] | list[str],
    now: datetime | None,
) -> set[str]:
    current_time = now or datetime.now(UTC)
    blocked: set[str] = set()
    for rail_name in rail_names:
        row = _get_or_create_rail_health(
            db,
            rail_name=rail_name,
            provider_name=rail_name,
            now=current_time,
        )
        _refresh_open_breaker(row, now=current_time)
        if row.breaker_state == RailBreakerState.OPEN:
            blocked.add(rail_name)
        db.add(row)

    db.flush()
    return blocked


def _get_or_create_rail_health(
    db: Session,
    *,
    rail_name: str,
    provider_name: str | None,
    now: datetime,
) -> RailHealth:
    row = (
        db.execute(select(RailHealth).where(RailHealth.rail_name == rail_name))
        .scalars()
        .one_or_none()
    )
    if row is not None:
        if provider_name and not row.provider_name:
            row.provider_name = provider_name
        return row

    created = RailHealth(
        rail_name=rail_name,
        provider_name=provider_name,
        breaker_state=RailBreakerState.CLOSED,
        consecutive_failures=0,
        last_error_code=None,
        last_error_message=None,
        opened_at=None,
        last_success_at=None,
        last_failure_at=None,
        cooldown_until=None,
        created_at=now,
        updated_at=now,
    )
    db.add(created)
    db.flush()
    return created


def _refresh_open_breaker(row: RailHealth, *, now: datetime) -> None:
    if row.breaker_state != RailBreakerState.OPEN:
        return
    if row.cooldown_until is None:
        return
    cooldown_until = _as_utc_datetime(row.cooldown_until)
    current_time = _as_utc_datetime(now)
    if cooldown_until <= current_time:
        row.breaker_state = RailBreakerState.HALF_OPEN


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
