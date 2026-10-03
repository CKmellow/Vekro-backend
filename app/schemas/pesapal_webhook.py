from pydantic import BaseModel, Field

from app.services.custody.enums import CollectionOutcome


class PesapalCollectionWebhookResponse(BaseModel):
    """Acknowledgement payload for Pesapal collection callbacks."""

    accepted: bool = Field(
        description="Whether the callback was accepted for processing and auditing."
    )
    duplicate: bool = Field(
        description="True when this callback was already processed for the dedupe key."
    )
    dedupe_key: str = Field(
        min_length=1,
        max_length=120,
        description="Provider dedupe key used to make callback handling idempotent.",
    )
    provider_reference: str | None = Field(
        default=None,
        description="Order tracking id parsed from callback payload when present.",
    )
    inquiry_outcome: CollectionOutcome | None = Field(
        default=None,
        description="Outcome from Pesapal status inquiry confirmation when available.",
    )
    detail: str = Field(description="Human-readable processing summary.")