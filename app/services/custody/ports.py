from typing import Protocol, runtime_checkable

from app.services.custody.dto import (
    CollectionResult,
    CustodyCapabilities,
    EscrowRecord,
    EscrowStatusResult,
    FundingRequest,
    OpenEscrowRequest,
    PayoutRequest,
    PayoutResult,
)


@runtime_checkable
class CollectionRail(Protocol):
    def request_funding(self, request: FundingRequest) -> CollectionResult: ...

    def get_funding_status(self, provider_reference: str) -> CollectionResult: ...


@runtime_checkable
class PayoutRail(Protocol):
    def request_payout(self, request: PayoutRequest) -> PayoutResult: ...

    def get_payout_status(self, provider_reference: str) -> PayoutResult: ...


@runtime_checkable
class CustodyProvider(Protocol):
    def capabilities(self) -> CustodyCapabilities: ...

    def open_escrow(self, request: OpenEscrowRequest) -> EscrowRecord: ...

    def request_funding(self, request: FundingRequest) -> CollectionResult: ...

    def get_status(self, escrow_reference: str) -> EscrowStatusResult: ...

    def release(self, request: PayoutRequest) -> PayoutResult: ...

    def refund(self, request: PayoutRequest) -> PayoutResult: ...
