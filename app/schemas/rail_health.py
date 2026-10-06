from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.rail_health import RailBreakerState


class AdminRailHealthEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rail_name: str = Field(description="Configured rail name.")
    provider_name: str | None = Field(
        default=None,
        description="Provider identifier associated with this rail.",
    )
    breaker_state: RailBreakerState = Field(
        description="Current breaker state for the rail.",
    )
    consecutive_failures: int = Field(
        ge=0,
        description="Consecutive failure count tracked for breaker transitions.",
    )
    last_error_code: str | None = Field(
        default=None,
        description="Last observed rail error code.",
    )
    last_error_message: str | None = Field(
        default=None,
        description="Last observed rail error message.",
    )
    opened_at: datetime | None = Field(
        default=None,
        description="When the breaker most recently moved to OPEN.",
    )
    last_success_at: datetime | None = Field(
        default=None,
        description="Timestamp of the latest successful rail operation.",
    )
    last_failure_at: datetime | None = Field(
        default=None,
        description="Timestamp of the latest failed rail operation.",
    )
    cooldown_until: datetime | None = Field(
        default=None,
        description="Earliest timestamp when OPEN breaker can transition to HALF_OPEN.",
    )
    collection_available: bool = Field(
        description="Whether this rail is available for collection routes.",
    )
    payout_available: bool = Field(
        description="Whether this rail is available for payout routes.",
    )
    routable: bool = Field(
        description="Whether breaker state currently allows this rail to be selected.",
    )


class AdminRailHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    custody_mode: str = Field(description="Active custody mode value.")
    holds_funds_structurally: bool = Field(
        description="Provider capability indicating whether funds are structurally held.",
    )
    collection_priority: list[str] = Field(
        description="Configured collection rail priority order.",
    )
    payout_priority: list[str] = Field(
        description="Configured payout rail priority order.",
    )
    rails: list[AdminRailHealthEntry] = Field(
        description="Per-rail breaker and routability state.",
    )
