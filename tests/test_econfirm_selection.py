"""E5: honest custody-provider selection + effective cap."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.custody.selection import (
    PROVIDER_ECONFIRM,
    PROVIDER_SIMULATED,
    effective_max_principal,
    select_custody_provider,
)
from cryptography.fernet import Fernet


def _settings(**overrides):
    base = dict(
        econfirm_enabled=True,
        econfirm_api_key="ek_live",
        econfirm_code_encryption_key=Fernet.generate_key().decode(),
        econfirm_min_amount=Decimal("100"),
        econfirm_max_amount=Decimal("1000"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _select(amount, breaker_open=False, **settings_overrides):
    return select_custody_provider(
        settings=_settings(**settings_overrides),
        amount=Decimal(amount),
        econfirm_breaker_open=breaker_open,
    )


def test_selects_econfirm_in_band():
    sel = _select("500")
    assert sel.provider == PROVIDER_ECONFIRM
    assert sel.is_econfirm is True
    assert sel.reason == "econfirm_selected"


def test_disabled_falls_back():
    assert _select("500", econfirm_enabled=False).provider == PROVIDER_SIMULATED


def test_missing_api_key_falls_back():
    assert _select("500", econfirm_api_key="  ").reason == "econfirm_api_key_missing"


def test_invalid_encryption_key_falls_back():
    sel = _select("500", econfirm_code_encryption_key="not-a-key")
    assert sel.provider == PROVIDER_SIMULATED
    assert sel.reason == "econfirm_encryption_key_missing"


def test_open_breaker_falls_back():
    assert _select("500", breaker_open=True).reason == "econfirm_breaker_open"


def test_below_min_falls_back():
    sel = _select("50")
    assert sel.provider == PROVIDER_SIMULATED
    assert sel.reason.startswith("amount_below_min")


def test_above_max_falls_back():
    sel = _select("5000")
    assert sel.provider == PROVIDER_SIMULATED
    assert sel.reason.startswith("amount_above_max")


def test_boundaries_are_inclusive():
    assert _select("100").provider == PROVIDER_ECONFIRM
    assert _select("1000").provider == PROVIDER_ECONFIRM


def test_effective_max_principal_takes_the_lower():
    settings = _settings(econfirm_max_amount=Decimal("1000"))
    # eConfirm reports a higher cap -> keep the conservative Vekro guard.
    assert effective_max_principal(settings, Decimal("500000")) == Decimal("1000")
    # eConfirm reports a lower cap -> use it.
    assert effective_max_principal(settings, Decimal("750")) == Decimal("750")
    # Unknown cap -> Vekro guard.
    assert effective_max_principal(settings, None) == Decimal("1000")
