from decimal import Decimal

import pytest
from app.core.phone import normalize_phone
from app.models.user import UserRole
from app.schemas.auth import LoginRequest, RegisterUserRequest
from app.services.custody.dto import FundingRequest, PayoutRequest
from app.services.payment import PaymentRequest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "+254712345678"),
        ("254712345678", "+254712345678"),
        ("+254712345678", "+254712345678"),
        (" 07 12 345 678 ", "+254712345678"),
    ],
)
def test_normalize_phone_supports_kenyan_variants(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


def test_register_request_normalizes_phone_fields() -> None:
    payload = RegisterUserRequest(
        name="Buyer",
        phone="0712345678",
        role=UserRole.BUYER,
        password="strong-pass-123",
        mpesa_phone="254712345678",
    )

    assert payload.phone == "+254712345678"
    assert payload.mpesa_phone == "+254712345678"


def test_login_request_normalizes_phone_field() -> None:
    payload = LoginRequest(phone="07 12 345 678", password="strong-pass-123")

    assert payload.phone == "+254712345678"


def test_payment_request_normalizes_rail_phone() -> None:
    request = PaymentRequest(
        transaction_id="txn-123",
        amount="1000.00",
        phone_number="0712345678",
        account_reference="acct-123",
        transaction_desc="Escrow payment",
    )

    assert request.phone_number == "+254712345678"


def test_custody_requests_normalize_rail_phone_values() -> None:
    funding = FundingRequest(
        escrow_reference="esc-1",
        amount=Decimal("100.00"),
        phone_number="254712345678",
        account_reference="acct-1",
    )
    payout = PayoutRequest(
        escrow_reference="esc-1",
        amount=Decimal("100.00"),
        destination_phone="0712345678",
        purpose="release",
    )

    assert funding.phone_number == "+254712345678"
    assert payout.destination_phone == "+254712345678"


def test_normalize_phone_rejects_non_digit_characters() -> None:
    with pytest.raises(ValueError, match="Phone number must contain 7-15 digits"):
        normalize_phone("07A2345678")
