from datetime import datetime

from pydantic import BaseModel, Field


class AnchorBatchSummary(BaseModel):
    batch_id: int
    status: str = Field(description="built, submitted, confirmed, failed_retryable or halted.")
    root: str = Field(description="0x-prefixed keccak Merkle root.")
    first_seq: int
    last_seq: int
    leaf_count: int
    tx_hash: str | None = Field(default=None, description="Latest anchoring transaction hash.")
    attempts: int
    last_error: str | None = None
    block_number: int | None = None
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None


class AnchoringStatusResponse(BaseModel):
    enabled: bool = Field(description="Whether the worker is allowed to submit (ANCHOR_ENABLED).")
    chain_id: int
    network: str | None = Field(default=None, description="anvil, base-sepolia or base.")
    registry_address: str | None = None
    anchorer_address: str | None = Field(
        default=None, description="Address of the anchoring key. The key itself is never shown."
    )
    halted: bool = Field(description="A batch disagreed with the chain; anchoring has stopped.")
    halt_reason: str | None = None
    last_confirmed: AnchorBatchSummary | None = None
    in_flight: AnchorBatchSummary | None = None
    confirmed_batches: int
    unanchored_events: int = Field(description="Recorded events not yet in a batch.")
    oldest_unanchored_at: datetime | None = None
    server_time: datetime
