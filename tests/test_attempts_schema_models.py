from app.models.collection_attempt import CollectionAttempt
from app.models.payout_attempt import PayoutAttempt
from app.models.provider_event import ProviderEvent
from app.models.rail_health import RailBreakerState, RailHealth
from sqlalchemy.dialects.postgresql import JSONB


def _assert_jsonb_column(model, column_name: str) -> None:
    column = model.__table__.c[column_name]
    assert isinstance(column.type, JSONB)


def test_attempt_snapshots_are_jsonb() -> None:
    _assert_jsonb_column(CollectionAttempt, "request_snapshot")
    _assert_jsonb_column(CollectionAttempt, "response_snapshot")
    _assert_jsonb_column(PayoutAttempt, "request_snapshot")
    _assert_jsonb_column(PayoutAttempt, "response_snapshot")


def test_payout_attempt_partial_unique_index_for_non_failed_attempts() -> None:
    index = next(
        idx
        for idx in PayoutAttempt.__table__.indexes
        if idx.name == "ux_payout_attempts_one_non_failed_per_escrow_purpose"
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["escrow_id", "purpose"]

    where_clause = index.dialect_options["postgresql"]["where"]
    assert where_clause is not None
    assert "failed_definite" in str(where_clause)


def test_provider_events_has_dedupe_unique_index() -> None:
    index = next(
        idx
        for idx in ProviderEvent.__table__.indexes
        if idx.name == "ux_provider_events_provider_dedupe_key"
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["provider_name", "dedupe_key"]


def test_rail_health_defaults_closed_breaker_state() -> None:
    breaker_col = RailHealth.__table__.c["breaker_state"]

    assert RailBreakerState.CLOSED.value == "closed"
    assert breaker_col.default is not None
    assert breaker_col.server_default is not None
    assert "closed" in str(breaker_col.server_default.arg)
