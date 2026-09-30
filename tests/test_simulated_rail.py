from decimal import Decimal

from app.core.settings import Settings
from app.services.custody.dto import FundingRequest, PayoutRequest
from app.services.custody.enums import CollectionOutcome, PayoutOutcome
from app.services.custody.registry import build_custody_registry
from app.services.custody.simulated_rail import SimulatedRail


def _settings(**overrides) -> Settings:
    base = {
        "secret_key": "test-secret-key",
        "database_url": "postgresql+psycopg://user:pass@localhost/testdb",
        "alembic_database_url": "postgresql+psycopg://user:pass@localhost/testdb",
        "environment": "development",
        "custody_mode": "tier_2",
        "custody_collection_rail_priority": "simulated",
        "custody_payout_rail_priority": "simulated",
        "allow_live_payouts": False,
        "simulated_collection_default_scenario": "success",
        "simulated_payout_default_scenario": "success",
        "simulated_trigger_prefix": "sim:",
        "loop_enabled": False,
        "loop_base_url": "",
        "loop_client_id": "",
        "loop_client_secret": "",
        "loop_shortcode": "",
        "loop_passkey": "",
        "intasend_enabled": False,
        "intasend_base_url": "",
        "intasend_publishable_key": "",
        "intasend_secret_key": "",
        "intasend_webhook_secret": "",
        "econfirm_enabled": False,
        "econfirm_base_url": "",
        "econfirm_api_key": "",
        "econfirm_api_secret": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _funding_request(account_reference: str) -> FundingRequest:
    return FundingRequest(
        escrow_reference="escrow-123",
        amount=Decimal("250.00"),
        phone_number="+254712345678",
        account_reference=account_reference,
    )


def _payout_request(purpose: str) -> PayoutRequest:
    return PayoutRequest(
        escrow_reference="escrow-123",
        amount=Decimal("250.00"),
        destination_phone="+254712345678",
        purpose=purpose,
    )


def test_simulated_rail_uses_configured_defaults_without_trigger() -> None:
    rail = SimulatedRail(
        collection_default_scenario="failed_definite",
        payout_default_scenario="timeout",
    )

    funding_result = rail.request_funding(_funding_request("order-1"))
    payout_result = rail.request_payout(_payout_request("release"))

    assert funding_result.outcome == CollectionOutcome.FAILED_DEFINITE
    assert funding_result.raw_status == "declined"
    assert payout_result.outcome == PayoutOutcome.UNKNOWN
    assert payout_result.raw_status == "timeout"


def test_simulated_rail_trigger_injection_overrides_default_scenarios() -> None:
    rail = SimulatedRail(
        collection_default_scenario="failed_definite",
        payout_default_scenario="failed_definite",
    )

    funding_success = rail.request_funding(_funding_request("txn sim:success"))
    payout_failure = rail.request_payout(_payout_request("sim:failed_definite"))
    payout_duplicate = rail.request_payout(_payout_request("sim:duplicate"))

    assert funding_success.outcome == CollectionOutcome.SUCCEEDED
    assert funding_success.raw_status == "accepted"
    assert payout_failure.outcome == PayoutOutcome.FAILED_DEFINITE
    assert payout_failure.raw_status == "declined"
    assert payout_duplicate.outcome == PayoutOutcome.SUCCEEDED
    assert payout_duplicate.raw_status == "duplicate"


def test_timeout_and_failed_definite_are_distinguishable() -> None:
    rail = SimulatedRail()

    timeout_result = rail.request_funding(_funding_request("sim:timeout"))
    failed_result = rail.request_funding(_funding_request("sim:failure"))

    assert timeout_result.outcome == CollectionOutcome.UNKNOWN
    assert timeout_result.raw_status == "timeout"
    assert failed_result.outcome == CollectionOutcome.FAILED_DEFINITE
    assert failed_result.raw_status == "declined"


def test_out_of_order_status_progression_is_deterministic() -> None:
    rail = SimulatedRail()

    payout_request_result = rail.request_payout(_payout_request("sim:out_of_order"))
    first_status = rail.get_payout_status(payout_request_result.provider_reference)
    second_status = rail.get_payout_status(payout_request_result.provider_reference)
    third_status = rail.get_payout_status(payout_request_result.provider_reference)

    assert payout_request_result.outcome == PayoutOutcome.UNKNOWN
    assert payout_request_result.raw_status == "out_of_order"

    assert first_status.outcome == PayoutOutcome.UNKNOWN
    assert first_status.raw_status == "out_of_order_pending"

    assert second_status.outcome == PayoutOutcome.SUCCEEDED
    assert second_status.raw_status == "settled_after_out_of_order"

    assert third_status.outcome == PayoutOutcome.SUCCEEDED
    assert third_status.raw_status == "settled_after_out_of_order"


def test_registry_wires_simulated_rail_from_runtime_config() -> None:
    settings = _settings(
        simulated_collection_default_scenario="timeout",
        simulated_payout_default_scenario="failed_definite",
        simulated_trigger_prefix="force:",
    )

    registry = build_custody_registry(settings)
    collection_rail = registry.get_collection_rail("simulated")
    payout_rail = registry.get_payout_rail("simulated")

    assert isinstance(collection_rail, SimulatedRail)
    assert collection_rail is payout_rail

    default_collection = collection_rail.request_funding(_funding_request("normal"))
    triggered_collection = collection_rail.request_funding(_funding_request("force:success"))
    default_payout = payout_rail.request_payout(_payout_request("release"))
    triggered_payout = payout_rail.request_payout(_payout_request("force:timeout"))

    assert default_collection.outcome == CollectionOutcome.UNKNOWN
    assert triggered_collection.outcome == CollectionOutcome.SUCCEEDED
    assert default_payout.outcome == PayoutOutcome.FAILED_DEFINITE
    assert triggered_payout.outcome == PayoutOutcome.UNKNOWN
