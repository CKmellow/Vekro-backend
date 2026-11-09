from datetime import datetime
from typing import Literal

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


class ReservesStatus(BaseModel):
    """The latest reserves reconciliation for one custody provider (M24 B2)."""

    provider: str
    status: Literal["covered", "shortfall", "incomplete", "unavailable", "not_applicable"]
    method: str
    liability_total: str
    attested_total: str | None
    difference: str | None
    at: str


class LiabilitiesStatus(BaseModel):
    snapshot_id: int
    total: str
    leaf_count: int
    at: datetime
    anchored: bool


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
    payouts_gated: bool = Field(
        description="Payouts wait for their authorisation to be anchored (ANCHOR_GATE_PAYOUTS)."
    )
    payouts_waiting_for_anchor: int = Field(
        description="Queued payouts held until their authorisation is anchored or readable."
    )
    oldest_payout_waiting_since: datetime | None = None
    payouts_halted: int = Field(
        description="Payouts stopped because they disagree with their anchored authorisation."
    )
    liabilities: LiabilitiesStatus | None = Field(
        default=None, description="The latest liabilities snapshot (what Vekro owes buyers)."
    )
    reserves: list[ReservesStatus] = Field(
        default_factory=list, description="The latest reserves reconciliation per provider."
    )
    reserves_shortfall: bool = Field(
        default=False, description="A provider attests less than Vekro owes: investigate now."
    )
    server_time: datetime


class ProofStepResponse(BaseModel):
    sibling: str = Field(description="0x-prefixed 32-byte sibling hash.")
    side: Literal["left", "right"] = Field(description="Where the sibling sits.")


class EventAnchorResponse(BaseModel):
    batch_id: int
    seq: int = Field(description="Gapless global position of the event.")
    leaf_index: int
    leaf_count: int
    batch_root: str = Field(
        description="Vekro's copy of the batch root. A verifier reads the root from the registry."
    )
    batch_status: str
    tx_hash: str | None = None
    block_number: int | None = None
    proof: list[ProofStepResponse] = Field(description="RFC 6962 inclusion proof, leaf upwards.")


class VerificationEventResponse(BaseModel):
    event_type: str
    schema_version: int
    payload: str = Field(description="Canonical JSON (RFC 8785), exactly as hashed.")
    salt: str
    prev_event_hash: str
    leaf_hash: str = Field(description="keccak256(0x00 || salt || prev_event_hash || payload).")
    created_at: datetime
    anchor: EventAnchorResponse | None = Field(
        default=None, description="Null while the event waits for a batch."
    )


class VerificationChainResponse(BaseModel):
    chain_id: int
    network: str | None = None
    registry_address: str | None = Field(
        default=None,
        description="Informational. Verifiers use the registry address they were built with.",
    )


class VerificationCurrentResponse(BaseModel):
    status: str = Field(description="Live transaction status, to compare with the last event.")
    amount: str = Field(description="Live amount as a 2-place decimal string.")


class TransactionVerificationResponse(BaseModel):
    version: int
    transaction_id: str
    reference: str
    chain: VerificationChainResponse
    current: VerificationCurrentResponse
    events: list[VerificationEventResponse] = Field(description="The order's chain, oldest first.")
    linked_events: list[VerificationEventResponse] = Field(
        description="Provider events from the global chain that this order's chain links to."
    )
    server_time: datetime


class PublicBatchResponse(BaseModel):
    batch_id: int
    root: str
    prev_root: str
    first_seq: int
    last_seq: int
    leaf_count: int
    status: str
    tx_hash: str | None = None
    block_number: int | None = None
    confirmed_at: datetime | None = None
    chain_id: int
    network: str | None = None
    registry_address: str


class SellerTrackRecordResponse(BaseModel):
    seller_id: str
    chain: VerificationChainResponse
    completed_sales: int = Field(description="Completed sales in Vekro's records (unverified).")
    recorded_sales: int = Field(description="Of those, how many have a track-record event.")
    sales: list[VerificationEventResponse] = Field(
        description=(
            "Newest first, at most 200: each sale's public payload (seller, outcome, amount band, "
            "unlinkable sale_ref) with its proof. Count as verified only the sales whose proof "
            "matches the registry root and whose payload names this seller."
        )
    )
    server_time: datetime


class SumNodeResponse(BaseModel):
    hash: str
    sum: str = Field(description="Cents, as a decimal string (may exceed 2**53).")


class SumLeafResponse(BaseModel):
    salt: str
    order_commit: str = Field(
        description='keccak256("vekro-liability-order:" + the order id): recompute it to check '
        "the leaf is this order's."
    )
    amount_cents: str


class SumProofStepResponse(BaseModel):
    sibling_hash: str
    sibling_sum: str
    side: Literal["left", "right"]


class LiabilitiesProofResponse(BaseModel):
    """Proof that this order's held amount is counted in Vekro's anchored total (M24 B2)."""

    snapshot_id: int
    created_at: datetime
    root: SumNodeResponse
    total: str = Field(description="KES held for all buyers at the snapshot.")
    leaf_count: int
    leaf: SumLeafResponse
    proof: list[SumProofStepResponse]
    anchor: VerificationEventResponse | None = Field(
        description="The liabilities_snapshot event in the global chain, with its batch proof; "
        "null until anchoring is on."
    )
