from pydantic import BaseModel, Field

from app.services.custody.enums import CollectionOutcome


class LoopCollectionWebhookResponse(BaseModel):
    """Acknowledgement payload for LOOP collection callbacks."""

    accepted: bool = Field(
        description="Whether the callback was accepted for processing and auditing."
    )
    duplicate: bool = Field(
        description="True when this callback was already processed for the dedupe key."
    )
    signature_valid: bool = Field(description="Whether callback signature verification passed.")
    dedupe_key: str = Field(
        min_length=1,
        max_length=120,
        description="Provider dedupe key used to make callback handling idempotent.",
    )
    provider_reference: str | None = Field(
        default=None,
        description="Provider transaction reference parsed from callback payload.",
    )
    inquiry_outcome: CollectionOutcome | None = Field(
        default=None,
        description="Outcome from LOOP inquiry confirmation when available.",
    )
    detail: str = Field(description="Human-readable processing summary.")
