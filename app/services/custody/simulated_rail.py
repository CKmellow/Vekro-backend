import re
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from app.services.custody.dto import (
    CollectionResult,
    FundingRequest,
    PayoutRequest,
    PayoutResult,
)
from app.services.custody.enums import CollectionOutcome, PayoutOutcome

SUPPORTED_SIMULATED_SCENARIOS = frozenset(
    {
        "success",
        "failed_definite",
        "timeout",
        "malformed",
        "duplicate",
        "out_of_order",
        "unknown",
    }
)

SCENARIO_ALIASES = {
    "succeeded": "success",
    "ok": "success",
    "failed": "failed_definite",
    "failure": "failed_definite",
    "declined": "failed_definite",
    "out-of-order": "out_of_order",
}


def normalize_simulated_scenario(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    normalized = SCENARIO_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_SIMULATED_SCENARIOS:
        raise ValueError(
            "Unsupported simulated scenario "
            f"'{value}'. Expected one of: {', '.join(sorted(SUPPORTED_SIMULATED_SCENARIOS))}."
        )
    return normalized


@dataclass
class SimulatedRail:
    collection_default_scenario: str = "success"
    payout_default_scenario: str = "success"
    trigger_prefix: str = "sim:"

    def __post_init__(self) -> None:
        self.collection_default_scenario = normalize_simulated_scenario(
            self.collection_default_scenario
        )
        self.payout_default_scenario = normalize_simulated_scenario(self.payout_default_scenario)

        normalized_prefix = self.trigger_prefix.strip().lower()
        if not normalized_prefix:
            raise ValueError("Simulated trigger prefix must not be blank.")
        self.trigger_prefix = normalized_prefix

        self._funding_status_checks: dict[str, int] = {}
        self._payout_status_checks: dict[str, int] = {}

    def request_funding(self, request: FundingRequest) -> CollectionResult:
        scenario = self._resolve_scenario(
            request.account_reference,
            self.collection_default_scenario,
        )
        provider_reference = self._build_provider_reference(
            kind="collect",
            escrow_reference=request.escrow_reference,
            discriminator=request.account_reference,
            scenario=scenario,
        )
        return self._collection_result(
            scenario=scenario,
            provider_reference=provider_reference,
            status_inquiry=False,
        )

    def get_funding_status(self, provider_reference: str) -> CollectionResult:
        scenario = self._scenario_from_provider_reference(
            provider_reference,
            fallback=self.collection_default_scenario,
        )
        return self._collection_result(
            scenario=scenario,
            provider_reference=provider_reference,
            status_inquiry=True,
        )

    def request_payout(self, request: PayoutRequest) -> PayoutResult:
        scenario = self._resolve_scenario(request.purpose, self.payout_default_scenario)
        provider_reference = self._build_provider_reference(
            kind="payout",
            escrow_reference=request.escrow_reference,
            discriminator=request.purpose,
            scenario=scenario,
        )
        return self._payout_result(
            scenario=scenario,
            provider_reference=provider_reference,
            status_inquiry=False,
        )

    def get_payout_status(self, provider_reference: str) -> PayoutResult:
        scenario = self._scenario_from_provider_reference(
            provider_reference,
            fallback=self.payout_default_scenario,
        )
        return self._payout_result(
            scenario=scenario,
            provider_reference=provider_reference,
            status_inquiry=True,
        )

    def _resolve_scenario(self, source: str, default_scenario: str) -> str:
        match = re.search(
            rf"{re.escape(self.trigger_prefix)}(?P<scenario>[a-z0-9_-]+)",
            source.lower(),
        )
        if not match:
            return default_scenario

        try:
            return normalize_simulated_scenario(match.group("scenario"))
        except ValueError:
            return default_scenario

    @staticmethod
    def _build_provider_reference(
        *,
        kind: str,
        escrow_reference: str,
        discriminator: str,
        scenario: str,
    ) -> str:
        seed = f"{kind}:{escrow_reference}:{discriminator}:{scenario}"
        suffix = uuid5(NAMESPACE_URL, seed).hex[:16]
        return f"sim:{kind}:{scenario}:{suffix}"

    @staticmethod
    def _scenario_from_provider_reference(provider_reference: str, fallback: str) -> str:
        parts = provider_reference.split(":")
        if len(parts) < 4 or parts[0] != "sim":
            return fallback
        try:
            return normalize_simulated_scenario(parts[2])
        except ValueError:
            return fallback

    def _collection_result(
        self,
        *,
        scenario: str,
        provider_reference: str,
        status_inquiry: bool,
    ) -> CollectionResult:
        outcome, raw_status, message = self._scenario_payload(
            scenario=scenario,
            status_inquiry=status_inquiry,
            status_checks=self._funding_status_checks,
            success_outcome=CollectionOutcome.SUCCEEDED,
            failed_outcome=CollectionOutcome.FAILED_DEFINITE,
            unknown_outcome=CollectionOutcome.UNKNOWN,
            provider_reference=provider_reference,
        )
        return CollectionResult(
            outcome=cast(CollectionOutcome, outcome),
            provider_reference=provider_reference,
            raw_status=raw_status,
            message=message,
        )

    def _payout_result(
        self,
        *,
        scenario: str,
        provider_reference: str,
        status_inquiry: bool,
    ) -> PayoutResult:
        outcome, raw_status, message = self._scenario_payload(
            scenario=scenario,
            status_inquiry=status_inquiry,
            status_checks=self._payout_status_checks,
            success_outcome=PayoutOutcome.SUCCEEDED,
            failed_outcome=PayoutOutcome.FAILED_DEFINITE,
            unknown_outcome=PayoutOutcome.UNKNOWN,
            provider_reference=provider_reference,
        )
        return PayoutResult(
            outcome=cast(PayoutOutcome, outcome),
            provider_reference=provider_reference,
            raw_status=raw_status,
            message=message,
        )

    @staticmethod
    def _scenario_payload(
        *,
        scenario: str,
        status_inquiry: bool,
        status_checks: dict[str, int],
        success_outcome: CollectionOutcome | PayoutOutcome,
        failed_outcome: CollectionOutcome | PayoutOutcome,
        unknown_outcome: CollectionOutcome | PayoutOutcome,
        provider_reference: str,
    ) -> tuple[CollectionOutcome | PayoutOutcome, str, str]:
        if scenario == "success":
            if status_inquiry:
                return success_outcome, "settled", "Simulated rail reports settled outcome."
            return success_outcome, "accepted", "Simulated rail accepted request."

        if scenario == "failed_definite":
            return failed_outcome, "declined", "Simulated rail produced a definite failure."

        if scenario == "timeout":
            return unknown_outcome, "timeout", "Simulated rail timed out; finality is unknown."

        if scenario == "malformed":
            return (
                unknown_outcome,
                "malformed_response",
                "Simulated rail returned malformed payload semantics.",
            )

        if scenario == "unknown":
            return unknown_outcome, "unknown", "Simulated rail returned unknown finality."

        if scenario == "duplicate":
            if status_inquiry:
                return (
                    success_outcome,
                    "duplicate_confirmed",
                    "Simulated rail indicates duplicate request already processed.",
                )
            return (
                success_outcome,
                "duplicate",
                "Simulated rail detected duplicate request and treated it as idempotent.",
            )

        if scenario == "out_of_order":
            if not status_inquiry:
                return (
                    unknown_outcome,
                    "out_of_order",
                    "Simulated rail produced out-of-order callback behavior.",
                )

            checks = status_checks.get(provider_reference, 0) + 1
            status_checks[provider_reference] = checks
            if checks == 1:
                return (
                    unknown_outcome,
                    "out_of_order_pending",
                    "Simulated rail awaiting reconciliation after out-of-order callback.",
                )

            return (
                success_outcome,
                "settled_after_out_of_order",
                "Simulated rail reconciled previously out-of-order flow.",
            )

        return unknown_outcome, "unknown", "Simulated rail returned unknown scenario state."
