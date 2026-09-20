from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentRequest:
    transaction_id: str
    amount: str
    phone_number: str | None
    account_reference: str
    transaction_desc: str


@dataclass(frozen=True)
class PaymentResponse:
    provider: str
    status: str
    provider_request_id: str
    merchant_request_id: str
    checkout_request_id: str
    customer_message: str


class PaymentGateway(Protocol):
    def initiate_stk_push(self, request: PaymentRequest) -> PaymentResponse: ...


class MpesaStkStubGateway:
    """Stubbed STK gateway that can be replaced by real Daraja transport later."""

    provider = "mpesa_stub"

    def initiate_stk_push(self, request: PaymentRequest) -> PaymentResponse:
        base = request.transaction_id.replace("-", "")[:12] or "txn"
        return PaymentResponse(
            provider=self.provider,
            status="queued",
            provider_request_id=f"stub-req-{base}",
            merchant_request_id=f"stub-merchant-{base}",
            checkout_request_id=f"stub-checkout-{base}",
            customer_message="STK push queued (stub).",
        )


def get_payment_gateway() -> PaymentGateway:
    return MpesaStkStubGateway()
