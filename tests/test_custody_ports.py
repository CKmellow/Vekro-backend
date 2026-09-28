from decimal import Decimal

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
from app.services.custody.enums import CollectionOutcome, CustodyMode, PayoutOutcome
from app.services.custody.ports import CollectionRail, CustodyProvider, PayoutRail
from app.services.custody.registry import CustodyRegistry


class StubCollectionRail:
    def request_funding(self, request: FundingRequest) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=f"collect-{request.escrow_reference}",
            raw_status="accepted",
            message="Funding queued.",
        )

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="settled",
            message="Funding confirmed.",
        )


class StubPayoutRail:
    def request_payout(self, request: PayoutRequest) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.SUCCEEDED,
            provider_reference=f"payout-{request.escrow_reference}",
            raw_status="accepted",
            message="Payout queued.",
        )

    def get_payout_status(self, provider_reference: str) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.SUCCEEDED,
            provider_reference=provider_reference,
            raw_status="settled",
            message="Payout confirmed.",
        )


class StubCustodyProvider:
    def __init__(self) -> None:
        self._collection_rail = StubCollectionRail()
        self._payout_rail = StubPayoutRail()

    def capabilities(self) -> CustodyCapabilities:
        return CustodyCapabilities(
            holds_funds_structurally=False,
            supports_split_payout=True,
            supports_partial_release=False,
            supports_webhook_auth=True,
        )

    def open_escrow(self, request: OpenEscrowRequest) -> EscrowRecord:
        return EscrowRecord(
            escrow_reference=f"escrow-{request.transaction_id}",
            transaction_id=request.transaction_id,
            amount=request.amount,
            currency=request.currency,
        )

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        return self._collection_rail.request_funding(request)

    def get_status(self, escrow_reference: str) -> EscrowStatusResult:
        return EscrowStatusResult(
            escrow_reference=escrow_reference,
            funded_amount=Decimal("1200.00"),
            released_amount=Decimal("0.00"),
            refunded_amount=Decimal("0.00"),
        )

    def release(self, request: PayoutRequest) -> PayoutResult:
        return self._payout_rail.request_payout(request)

    def refund(self, request: PayoutRequest) -> PayoutResult:
        return self._payout_rail.request_payout(request)


def test_payout_outcome_enum_values_are_stable() -> None:
    assert PayoutOutcome.SUCCEEDED.value == "SUCCEEDED"
    assert PayoutOutcome.FAILED_DEFINITE.value == "FAILED_DEFINITE"
    assert PayoutOutcome.UNKNOWN.value == "UNKNOWN"


def test_collection_outcome_enum_values_are_stable() -> None:
    assert CollectionOutcome.SUCCEEDED.value == "SUCCEEDED"
    assert CollectionOutcome.FAILED_DEFINITE.value == "FAILED_DEFINITE"
    assert CollectionOutcome.UNKNOWN.value == "UNKNOWN"


def test_contract_dto_serialization_uses_downstream_enum_values() -> None:
    funding_result = CollectionResult(
        outcome=CollectionOutcome.UNKNOWN,
        provider_reference="collect-123",
        raw_status="pending",
        message="Awaiting inquiry.",
    )
    payout_result = PayoutResult(
        outcome=PayoutOutcome.FAILED_DEFINITE,
        provider_reference="payout-123",
        raw_status="declined",
        message="Account rejected.",
    )
    capabilities = CustodyCapabilities(
        holds_funds_structurally=False,
        supports_split_payout=True,
        supports_partial_release=False,
        supports_webhook_auth=True,
    )

    assert funding_result.to_payload()["outcome"] == "UNKNOWN"
    assert payout_result.to_payload()["outcome"] == "FAILED_DEFINITE"
    assert capabilities.to_payload() == {
        "holds_funds_structurally": False,
        "supports_split_payout": True,
        "supports_partial_release": False,
        "supports_webhook_auth": True,
    }


def test_ports_compile_and_registry_imports_contracts() -> None:
    provider = StubCustodyProvider()
    collection_rail = StubCollectionRail()
    payout_rail = StubPayoutRail()

    assert isinstance(provider, CustodyProvider)
    assert isinstance(collection_rail, CollectionRail)
    assert isinstance(payout_rail, PayoutRail)

    registry = CustodyRegistry(
        provider=provider,
        collection_rails={"simulated": collection_rail},
        payout_rails={"simulated": payout_rail},
        custody_mode=CustodyMode.TIER_2,
        collection_priority=("simulated",),
        payout_priority=("simulated",),
        live_payouts_enabled=False,
    )

    assert registry.provider.capabilities().supports_split_payout is True
    assert registry.get_collection_rail("simulated") is collection_rail
    assert registry.get_payout_rail("simulated") is payout_rail
