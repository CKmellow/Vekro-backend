import pytest
from app.core.settings import Settings
from app.services.custody.enums import CustodyMode
from app.services.custody.loop_payout import LoopPayoutRail
from app.services.custody.registry import build_custody_registry
from pydantic import ValidationError


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
        "loop_enabled": False,
        "loop_base_url": "",
        "loop_client_id": "",
        "loop_client_secret": "",
        "loop_shortcode": "",
        "loop_passkey": "",
        "pesapal_enabled": False,
        "pesapal_base_url": "",
        "pesapal_consumer_key": "",
        "pesapal_consumer_secret": "",
        "pesapal_callback_url": "",
        "pesapal_ipn_id": "",
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
    return Settings.model_validate(base)


def test_unknown_rail_in_priority_fails_fast() -> None:
    with pytest.raises(ValidationError, match="Unknown rails in custody priorities"):
        _settings(custody_collection_rail_priority="simulated,ghost")


def test_enabled_loop_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="LOOP_BASE_URL"):
        _settings(
            loop_enabled=True,
            custody_collection_rail_priority="simulated,loop",
            custody_payout_rail_priority="simulated,loop",
        )


def test_enabled_pesapal_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="PESAPAL_BASE_URL"):
        _settings(
            pesapal_enabled=True,
            custody_collection_rail_priority="simulated,pesapal",
        )


def test_enabled_intasend_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="INTASEND_BASE_URL"):
        _settings(
            intasend_enabled=True,
            custody_collection_rail_priority="simulated,intasend",
            custody_payout_rail_priority="simulated,intasend",
        )


def test_pesapal_is_rejected_in_payout_priority() -> None:
    with pytest.raises(ValidationError, match="collection-only"):
        _settings(
            pesapal_enabled=True,
            pesapal_base_url="https://cybqa.pesapal.com/pesapalv3",
            pesapal_consumer_key="pesapal-key",
            pesapal_consumer_secret="pesapal-secret",
            pesapal_callback_url="https://example.test/api/webhooks/pesapal/callback",
            custody_collection_rail_priority="simulated,pesapal",
            custody_payout_rail_priority="simulated,pesapal",
        )


def test_live_payouts_flag_is_rejected_outside_production() -> None:
    with pytest.raises(ValidationError, match="ALLOW_LIVE_PAYOUTS"):
        _settings(allow_live_payouts=True)


def test_invalid_simulated_default_scenario_fails_validation() -> None:
    with pytest.raises(ValidationError, match="simulated_collection_default_scenario"):
        _settings(simulated_collection_default_scenario="nonsense")


def test_registry_uses_priority_order_and_blocks_non_simulated_payouts_by_default() -> None:
    settings = _settings(
        loop_enabled=True,
        loop_base_url="https://sandbox.loop.example",
        loop_client_id="loop-client",
        loop_client_secret="loop-secret",
        loop_shortcode="600111",
        loop_passkey="loop-passkey",
        custody_collection_rail_priority="loop,simulated",
        custody_payout_rail_priority="simulated,loop",
    )

    registry = build_custody_registry(settings)

    assert registry.custody_mode == CustodyMode.TIER_2
    assert registry.collection_priority == ("loop", "simulated")
    assert registry.payout_priority == ("simulated", "loop")
    assert registry.live_payouts_enabled is False
    assert registry.get_collection_rail("loop") is registry.collection_rails["loop"]
    assert registry.get_payout_rail("simulated") is registry.payout_rails["simulated"]

    with pytest.raises(RuntimeError, match="Live payouts are blocked"):
        registry.get_payout_rail("loop")


def test_disabled_rails_are_skipped_without_runtime_exception() -> None:
    settings = _settings(
        pesapal_enabled=True,
        pesapal_base_url="https://cybqa.pesapal.com/pesapalv3",
        pesapal_consumer_key="pesapal-key",
        pesapal_consumer_secret="pesapal-secret",
        pesapal_callback_url="https://example.test/api/webhooks/pesapal/callback",
        custody_collection_rail_priority="loop,pesapal,simulated",
        custody_payout_rail_priority="loop,simulated",
    )

    registry = build_custody_registry(settings)

    assert registry.collection_priority == ("loop", "pesapal", "simulated")
    assert registry.payout_priority == ("loop", "simulated")
    assert registry.ordered_collection_rail_names() == ("pesapal", "simulated")
    assert registry.ordered_payout_rail_names() == ("simulated",)
    assert registry.select_collection_rail()[0] == "pesapal"
    assert registry.select_payout_rail()[0] == "simulated"

    with pytest.raises(RuntimeError, match="not available at runtime"):
        registry.get_collection_rail("loop")


def test_collection_fallback_order_is_deterministic() -> None:
    settings = _settings(
        loop_enabled=True,
        loop_base_url="https://sandbox.loop.example",
        loop_client_id="loop-client",
        loop_client_secret="loop-secret",
        loop_shortcode="600111",
        loop_passkey="loop-passkey",
        pesapal_enabled=True,
        pesapal_base_url="https://cybqa.pesapal.com/pesapalv3",
        pesapal_consumer_key="pesapal-key",
        pesapal_consumer_secret="pesapal-secret",
        pesapal_callback_url="https://example.test/api/webhooks/pesapal/callback",
        custody_collection_rail_priority="loop,pesapal,simulated",
    )

    registry = build_custody_registry(settings)

    assert registry.ordered_collection_rail_names() == ("loop", "pesapal", "simulated")
    assert registry.ordered_collection_rail_names(exclude={"loop"}) == ("pesapal", "simulated")
    assert registry.ordered_collection_rail_names(exclude={"loop", "pesapal"}) == (
        "simulated",
    )
    assert registry.select_collection_rail(exclude={"loop"})[0] == "pesapal"


def test_registry_fails_when_no_enabled_rails_are_available() -> None:
    settings = _settings(
        custody_collection_rail_priority="loop,pesapal",
        custody_payout_rail_priority="loop,intasend",
    )

    with pytest.raises(ValueError, match="No enabled collection rails"):
        build_custody_registry(settings)


def test_production_mode_can_enable_live_payout_rails() -> None:
    settings = _settings(
        environment="production",
        allow_live_payouts=True,
        session_cookie_secure=True,
        csrf_cookie_secure=True,
        frontend_url="https://frontend.example",
        cors_origins="https://frontend.example",
        loop_enabled=True,
        loop_base_url="https://sandbox.loop.example",
        loop_client_id="loop-client",
        loop_client_secret="loop-secret",
        loop_shortcode="600111",
        loop_passkey="loop-passkey",
        custody_payout_rail_priority="loop,simulated",
    )

    registry = build_custody_registry(settings)

    assert registry.live_payouts_enabled is True
    assert registry.get_payout_rail("loop") is registry.payout_rails["loop"]
    assert isinstance(registry.payout_rails["loop"], LoopPayoutRail)
