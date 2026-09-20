from app.services.payment import MpesaStkStubGateway, PaymentRequest


def test_mpesa_stub_gateway_returns_queued_response() -> None:
    gateway = MpesaStkStubGateway()

    response = gateway.initiate_stk_push(
        PaymentRequest(
            transaction_id="7a0d6f3f-8733-4df8-8e70-2632d8da9db4",
            amount="1500.00",
            phone_number="+254700000001",
            account_reference="7a0d6f3f-8733-4df8-8e70-2632d8da9db4",
            transaction_desc="Escrow payment",
        )
    )

    assert response.provider == "mpesa_stub"
    assert response.status == "queued"
    assert response.provider_request_id.startswith("stub-req-")
    assert response.merchant_request_id.startswith("stub-merchant-")
    assert response.checkout_request_id.startswith("stub-checkout-")
    assert response.customer_message == "STK push queued (stub)."
