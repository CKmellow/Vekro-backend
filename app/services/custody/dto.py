from dataclasses import dataclass
from decimal import Decimal

from app.core.phone import normalize_phone
from app.services.custody.enums import CollectionOutcome, PayoutOutcome


@dataclass(frozen=True)
class CustodyCapabilities:
    holds_funds_structurally: bool
    supports_split_payout: bool = False
    supports_partial_release: bool = False
    supports_webhook_auth: bool = False

    def to_payload(self) -> dict[str, bool]:
        return {
            "holds_funds_structurally": self.holds_funds_structurally,
            "supports_split_payout": self.supports_split_payout,
            "supports_partial_release": self.supports_partial_release,
            "supports_webhook_auth": self.supports_webhook_auth,
        }


@dataclass(frozen=True)
class OpenEscrowRequest:
    transaction_id: str
    buyer_id: str
    seller_id: str
    amount: Decimal
    currency: str = "KES"

    def to_payload(self) -> dict[str, str]:
        return {
            "transaction_id": self.transaction_id,
            "buyer_id": self.buyer_id,
            "seller_id": self.seller_id,
            "amount": str(self.amount),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class EscrowRecord:
    escrow_reference: str
    transaction_id: str
    amount: Decimal
    currency: str

    def to_payload(self) -> dict[str, str]:
        return {
            "escrow_reference": self.escrow_reference,
            "transaction_id": self.transaction_id,
            "amount": str(self.amount),
            "currency": self.currency,
        }


@dataclass(frozen=True)
class FundingRequest:
    escrow_reference: str
    amount: Decimal
    phone_number: str
    account_reference: str
    currency: str = "KES"

    def __post_init__(self) -> None:
        object.__setattr__(self, "phone_number", normalize_phone(self.phone_number))

    def to_payload(self) -> dict[str, str]:
        return {
            "escrow_reference": self.escrow_reference,
            "amount": str(self.amount),
            "phone_number": self.phone_number,
            "account_reference": self.account_reference,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class CollectionResult:
    outcome: CollectionOutcome
    provider_reference: str
    raw_status: str | None = None
    message: str | None = None
    metadata: dict[str, str | None] | None = None

    def to_payload(self) -> dict[str, str | dict[str, str | None] | None]:
        return {
            "outcome": self.outcome.value,
            "provider_reference": self.provider_reference,
            "raw_status": self.raw_status,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PayoutRequest:
    escrow_reference: str
    amount: Decimal
    destination_phone: str
    purpose: str
    currency: str = "KES"

    def __post_init__(self) -> None:
        object.__setattr__(self, "destination_phone", normalize_phone(self.destination_phone))

    def to_payload(self) -> dict[str, str]:
        return {
            "escrow_reference": self.escrow_reference,
            "amount": str(self.amount),
            "destination_phone": self.destination_phone,
            "purpose": self.purpose,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class PayoutResult:
    outcome: PayoutOutcome
    provider_reference: str
    raw_status: str | None = None
    message: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        return {
            "outcome": self.outcome.value,
            "provider_reference": self.provider_reference,
            "raw_status": self.raw_status,
            "message": self.message,
        }


@dataclass(frozen=True)
class EscrowStatusResult:
    escrow_reference: str
    funded_amount: Decimal
    released_amount: Decimal
    refunded_amount: Decimal

    def to_payload(self) -> dict[str, str]:
        return {
            "escrow_reference": self.escrow_reference,
            "funded_amount": str(self.funded_amount),
            "released_amount": str(self.released_amount),
            "refunded_amount": str(self.refunded_amount),
        }
