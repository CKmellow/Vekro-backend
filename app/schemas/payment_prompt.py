from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.payment_prompt import PaymentPromptKind
from app.schemas.transaction import TransactionResponse
from app.services.payment_prompts import PaymentPromptState

PaymentKind = Literal["STK_PROMPT", "REDIRECT"]
PaymentStatus = Literal["pending", "succeeded", "failed", "expired", "failed_to_initiate"]
PaymentFailure = Literal[
    "cancelled", "wrong_pin", "insufficient_funds", "timeout", "provider_error"
]


class PaymentInstructionResponse(BaseModel):
    """What the buyer was asked to do to pay, and how it went. Names no payment provider."""

    kind: PaymentKind = Field(
        description="STK_PROMPT: a PIN prompt was sent to the buyer's phone. "
        "REDIRECT: send the buyer to redirect_url."
    )
    redirect_url: str | None = Field(default=None, description="Only when kind is REDIRECT.")
    status: PaymentStatus = Field(
        description="expired: no answer before expires_at. Success is only final when the "
        "transaction status is locked."
    )
    failure_reason: PaymentFailure | None = Field(
        default=None, description="Why the prompt failed, when status is failed."
    )
    sent_at: datetime
    expires_at: datetime
    resend_available_at: datetime = Field(
        description="Earliest time POST /transactions/{id}/payment/resend is accepted."
    )

    @classmethod
    def from_state(cls, state: PaymentPromptState) -> "PaymentInstructionResponse":
        return cls(
            kind="REDIRECT" if state.kind == PaymentPromptKind.REDIRECT else "STK_PROMPT",
            redirect_url=state.redirect_url,
            status=state.status,  # type: ignore[arg-type]
            failure_reason=state.failure_reason.value if state.failure_reason else None,  # type: ignore[arg-type]
            sent_at=state.sent_at,
            expires_at=state.expires_at,
            resend_available_at=state.resend_available_at,
        )


class TransactionCreatedResponse(TransactionResponse):
    """A new transaction and the payment prompt sent for it."""

    payment: PaymentInstructionResponse
