from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

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

SUPPORTED_RAILS = frozenset({"simulated", "loop", "intasend"})
LIVE_PAYOUT_RAILS = frozenset({"loop", "intasend"})


class CustodyRuntimeSettings(Protocol):
    custody_mode: str
    loop_enabled: bool
    intasend_enabled: bool
    live_payouts_enabled: bool
    custody_collection_rail_priority_list: list[str]
    custody_payout_rail_priority_list: list[str]


@dataclass(frozen=True)
class PlaceholderCollectionRail:
    rail_name: str

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.UNKNOWN,
            provider_reference=f"{self.rail_name}-collect-{request.escrow_reference}",
            raw_status="unimplemented",
            message=f"{self.rail_name} collection adapter is not implemented yet.",
        )

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.UNKNOWN,
            provider_reference=provider_reference,
            raw_status="unimplemented",
            message=f"{self.rail_name} collection status inquiry is not implemented yet.",
        )


@dataclass(frozen=True)
class PlaceholderPayoutRail:
    rail_name: str

    def request_payout(self, request: PayoutRequest) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            provider_reference=f"{self.rail_name}-payout-{request.escrow_reference}",
            raw_status="unimplemented",
            message=f"{self.rail_name} payout adapter is not implemented yet.",
        )

    def get_payout_status(self, provider_reference: str) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            provider_reference=provider_reference,
            raw_status="unimplemented",
            message=f"{self.rail_name} payout status inquiry is not implemented yet.",
        )


@dataclass(frozen=True)
class ConfiguredCustodyProvider:
    custody_mode: CustodyMode

    def capabilities(self) -> CustodyCapabilities:
        return CustodyCapabilities(
            holds_funds_structurally=self.custody_mode == CustodyMode.TIER_1,
            supports_split_payout=False,
            supports_partial_release=False,
            supports_webhook_auth=False,
        )

    def open_escrow(self, request: OpenEscrowRequest) -> EscrowRecord:
        return EscrowRecord(
            escrow_reference=f"{self.custody_mode.value}-{request.transaction_id}",
            transaction_id=request.transaction_id,
            amount=request.amount,
            currency=request.currency,
        )

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        return CollectionResult(
            outcome=CollectionOutcome.UNKNOWN,
            provider_reference=f"provider-collect-{request.escrow_reference}",
            raw_status="unimplemented",
            message="Funding orchestration not implemented for configured provider.",
        )

    def get_status(self, escrow_reference: str) -> EscrowStatusResult:
        return EscrowStatusResult(
            escrow_reference=escrow_reference,
            funded_amount=Decimal("0.00"),
            released_amount=Decimal("0.00"),
            refunded_amount=Decimal("0.00"),
        )

    def release(self, request: PayoutRequest) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            provider_reference=f"provider-release-{request.escrow_reference}",
            raw_status="unimplemented",
            message="Release orchestration not implemented for configured provider.",
        )

    def refund(self, request: PayoutRequest) -> PayoutResult:
        return PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            provider_reference=f"provider-refund-{request.escrow_reference}",
            raw_status="unimplemented",
            message="Refund orchestration not implemented for configured provider.",
        )


@dataclass(frozen=True)
class CustodyRegistry:
    provider: CustodyProvider
    collection_rails: Mapping[str, CollectionRail]
    payout_rails: Mapping[str, PayoutRail]
    custody_mode: CustodyMode
    collection_priority: tuple[str, ...]
    payout_priority: tuple[str, ...]
    live_payouts_enabled: bool

    def get_collection_rail(self, rail_name: str) -> CollectionRail:
        return self.collection_rails[rail_name]

    def get_payout_rail(self, rail_name: str) -> PayoutRail:
        if rail_name in LIVE_PAYOUT_RAILS and not self.live_payouts_enabled:
            raise RuntimeError(
                "Live payouts are blocked. Set ENVIRONMENT=production and "
                "ALLOW_LIVE_PAYOUTS=true to enable non-simulated payout rails."
            )
        return self.payout_rails[rail_name]


def _rail_enabled(rail_name: str, settings: CustodyRuntimeSettings) -> bool:
    if rail_name == "simulated":
        return True
    if rail_name == "loop":
        return settings.loop_enabled
    if rail_name == "intasend":
        return settings.intasend_enabled
    return False


def build_custody_registry(settings: CustodyRuntimeSettings) -> CustodyRegistry:
    collection_priority = tuple(settings.custody_collection_rail_priority_list)
    payout_priority = tuple(settings.custody_payout_rail_priority_list)

    unknown_collection = sorted(set(collection_priority).difference(SUPPORTED_RAILS))
    unknown_payout = sorted(set(payout_priority).difference(SUPPORTED_RAILS))
    if unknown_collection:
        raise ValueError("Unknown collection rails configured: " + ", ".join(unknown_collection))
    if unknown_payout:
        raise ValueError("Unknown payout rails configured: " + ", ".join(unknown_payout))

    disabled_collection = [
        rail_name for rail_name in collection_priority if not _rail_enabled(rail_name, settings)
    ]
    disabled_payout = [
        rail_name for rail_name in payout_priority if not _rail_enabled(rail_name, settings)
    ]
    if disabled_collection:
        raise ValueError(
            "Collection priorities include disabled rails: " + ", ".join(disabled_collection)
        )
    if disabled_payout:
        raise ValueError("Payout priorities include disabled rails: " + ", ".join(disabled_payout))

    collection_rails = {
        rail_name: PlaceholderCollectionRail(rail_name=rail_name)
        for rail_name in collection_priority
    }
    payout_rails = {
        rail_name: PlaceholderPayoutRail(rail_name=rail_name) for rail_name in payout_priority
    }

    return CustodyRegistry(
        provider=ConfiguredCustodyProvider(custody_mode=CustodyMode(settings.custody_mode)),
        collection_rails=collection_rails,
        payout_rails=payout_rails,
        custody_mode=CustodyMode(settings.custody_mode),
        collection_priority=collection_priority,
        payout_priority=payout_priority,
        live_payouts_enabled=settings.live_payouts_enabled,
    )
