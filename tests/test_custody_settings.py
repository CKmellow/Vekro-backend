import pytest
from app.core.settings import Settings
from app.services.custody.enums import CustodyMode
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


def test_enabled_intasend_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="INTASEND_BASE_URL"):
        _settings(
            intasend_enabled=True,
            custody_collection_rail_priority="simulated,intasend",
            custody_payout_rail_priority="simulated,intasend",
        )


def test_live_payouts_flag_is_rejected_outside_production() -> None:
    with pytest.raises(ValidationError, match="ALLOW_LIVE_PAYOUTS"):
        _settings(allow_live_payouts=True)


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
