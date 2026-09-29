"""m11 add attempts provider events and rail health

Revision ID: e7f99b6cb3ea
Revises: c4f1a9b92e16
Create Date: 2026-09-29 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7f99b6cb3ea"
down_revision: Union[str, Sequence[str], None] = "c4f1a9b92e16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


attempt_outcome_enum = sa.Enum(
    "succeeded",
    "failed_definite",
    "unknown",
    name="attempt_outcome",
)

rail_breaker_state_enum = sa.Enum(
    "closed",
    "open",
    "half_open",
    name="rail_breaker_state",
)


def upgrade() -> None:
    attempt_outcome_enum.create(op.get_bind(), checkfirst=True)
    rail_breaker_state_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "collection_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("escrow_id", sa.UUID(), nullable=False),
        sa.Column("rail_name", sa.String(length=40), nullable=False),
        sa.Column("provider_name", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("provider_reference", sa.String(length=120), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'KES'"), nullable=False),
        sa.Column(
            "outcome", attempt_outcome_enum, server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column(
            "request_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "response_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_collection_attempts_amount_positive"),
        sa.ForeignKeyConstraint(["escrow_id"], ["escrows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_collection_attempts_escrow_id"), "collection_attempts", ["escrow_id"])
    op.create_index(op.f("ix_collection_attempts_rail_name"), "collection_attempts", ["rail_name"])
    op.create_index(
        op.f("ix_collection_attempts_provider_name"), "collection_attempts", ["provider_name"]
    )
    op.create_index(
        op.f("ix_collection_attempts_idempotency_key"), "collection_attempts", ["idempotency_key"]
    )
    op.create_index(
        op.f("ix_collection_attempts_provider_reference"),
        "collection_attempts",
        ["provider_reference"],
    )
    op.create_index(op.f("ix_collection_attempts_outcome"), "collection_attempts", ["outcome"])

    op.create_table(
        "payout_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("escrow_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("rail_name", sa.String(length=40), nullable=False),
        sa.Column("provider_name", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("provider_reference", sa.String(length=120), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default=sa.text("'KES'"), nullable=False),
        sa.Column(
            "outcome", attempt_outcome_enum, server_default=sa.text("'unknown'"), nullable=False
        ),
        sa.Column(
            "request_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "response_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payout_attempts_amount_positive"),
        sa.ForeignKeyConstraint(["escrow_id"], ["escrows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payout_attempts_escrow_id"), "payout_attempts", ["escrow_id"])
    op.create_index(op.f("ix_payout_attempts_purpose"), "payout_attempts", ["purpose"])
    op.create_index(op.f("ix_payout_attempts_rail_name"), "payout_attempts", ["rail_name"])
    op.create_index(op.f("ix_payout_attempts_provider_name"), "payout_attempts", ["provider_name"])
    op.create_index(
        op.f("ix_payout_attempts_idempotency_key"), "payout_attempts", ["idempotency_key"]
    )
    op.create_index(
        op.f("ix_payout_attempts_provider_reference"), "payout_attempts", ["provider_reference"]
    )
    op.create_index(op.f("ix_payout_attempts_outcome"), "payout_attempts", ["outcome"])
    op.create_index(
        "ux_payout_attempts_one_non_failed_per_escrow_purpose",
        "payout_attempts",
        ["escrow_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("outcome <> 'failed_definite'"),
    )

    op.create_table(
        "provider_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider_name", sa.String(length=40), nullable=False),
        sa.Column("rail_name", sa.String(length=40), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column("external_event_id", sa.String(length=120), nullable=True),
        sa.Column(
            "request_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "response_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_events_provider_name"), "provider_events", ["provider_name"])
    op.create_index(op.f("ix_provider_events_rail_name"), "provider_events", ["rail_name"])
    op.create_index(op.f("ix_provider_events_event_type"), "provider_events", ["event_type"])
    op.create_index(
        op.f("ix_provider_events_external_event_id"), "provider_events", ["external_event_id"]
    )
    op.create_index(
        "ux_provider_events_provider_dedupe_key",
        "provider_events",
        ["provider_name", "dedupe_key"],
        unique=True,
    )

    op.create_table(
        "rail_health",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("rail_name", sa.String(length=40), nullable=False),
        sa.Column("provider_name", sa.String(length=40), nullable=True),
        sa.Column(
            "breaker_state",
            rail_breaker_state_enum,
            server_default=sa.text("'closed'"),
            nullable=False,
        ),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=255), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_rail_health_consecutive_failures_non_negative",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rail_health_rail_name"), "rail_health", ["rail_name"], unique=True)
    op.create_index(op.f("ix_rail_health_provider_name"), "rail_health", ["provider_name"])
    op.create_index(op.f("ix_rail_health_breaker_state"), "rail_health", ["breaker_state"])


def downgrade() -> None:
    op.drop_index(op.f("ix_rail_health_breaker_state"), table_name="rail_health")
    op.drop_index(op.f("ix_rail_health_provider_name"), table_name="rail_health")
    op.drop_index(op.f("ix_rail_health_rail_name"), table_name="rail_health")
    op.drop_table("rail_health")

    op.drop_index("ux_provider_events_provider_dedupe_key", table_name="provider_events")
    op.drop_index(op.f("ix_provider_events_external_event_id"), table_name="provider_events")
    op.drop_index(op.f("ix_provider_events_event_type"), table_name="provider_events")
    op.drop_index(op.f("ix_provider_events_rail_name"), table_name="provider_events")
    op.drop_index(op.f("ix_provider_events_provider_name"), table_name="provider_events")
    op.drop_table("provider_events")

    op.drop_index(
        "ux_payout_attempts_one_non_failed_per_escrow_purpose",
        table_name="payout_attempts",
    )
    op.drop_index(op.f("ix_payout_attempts_outcome"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_provider_reference"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_idempotency_key"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_provider_name"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_rail_name"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_purpose"), table_name="payout_attempts")
    op.drop_index(op.f("ix_payout_attempts_escrow_id"), table_name="payout_attempts")
    op.drop_table("payout_attempts")

    op.drop_index(op.f("ix_collection_attempts_outcome"), table_name="collection_attempts")
    op.drop_index(
        op.f("ix_collection_attempts_provider_reference"), table_name="collection_attempts"
    )
    op.drop_index(op.f("ix_collection_attempts_idempotency_key"), table_name="collection_attempts")
    op.drop_index(op.f("ix_collection_attempts_provider_name"), table_name="collection_attempts")
    op.drop_index(op.f("ix_collection_attempts_rail_name"), table_name="collection_attempts")
    op.drop_index(op.f("ix_collection_attempts_escrow_id"), table_name="collection_attempts")
    op.drop_table("collection_attempts")

    rail_breaker_state_enum.drop(op.get_bind(), checkfirst=True)
    attempt_outcome_enum.drop(op.get_bind(), checkfirst=True)
