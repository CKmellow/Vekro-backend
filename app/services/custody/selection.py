"""Honest custody-provider selection for opening a new escrow (M20 / E5).

Decides whether a *new* escrow opens on eConfirm (Tier 1, real custody) or falls
back to the simulated ledger (Tier 2), applying the M20 fallback and amount
rules. The decision is honest: any condition that would make eConfirm unusable
(disabled, missing/invalid key, open breaker, amount outside the guard band)
falls back to simulated with a recorded reason, so the API/UI never presents a
simulated escrow as real money (Hard Rule 2).

This is a pure function so it is trivially testable and has no side effects.
Stickiness, persistence and routing of the chosen provider live in the
orchestration layer and the executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.services.custody.econfirm_crypto import is_valid_key_config

PROVIDER_ECONFIRM = "econfirm"
PROVIDER_SIMULATED = "simulated"


class _SelectionSettings(Protocol):
    econfirm_enabled: bool
    econfirm_api_key: str
    econfirm_code_encryption_key: str
    econfirm_min_amount: Decimal
    econfirm_max_amount: Decimal


@dataclass(frozen=True)
class CustodySelection:
    provider: str
    reason: str

    @property
    def is_econfirm(self) -> bool:
        return self.provider == PROVIDER_ECONFIRM


def select_custody_provider(
    *,
    settings: _SelectionSettings,
    amount: Decimal,
    econfirm_breaker_open: bool,
) -> CustodySelection:
    """Pick the custody provider for a brand-new escrow (before any open call)."""
    if not settings.econfirm_enabled:
        return CustodySelection(PROVIDER_SIMULATED, "econfirm_disabled")
    if not settings.econfirm_api_key.strip():
        return CustodySelection(PROVIDER_SIMULATED, "econfirm_api_key_missing")
    if not is_valid_key_config(settings.econfirm_code_encryption_key):
        return CustodySelection(PROVIDER_SIMULATED, "econfirm_encryption_key_missing")
    if econfirm_breaker_open:
        return CustodySelection(PROVIDER_SIMULATED, "econfirm_breaker_open")

    min_amount = Decimal(settings.econfirm_min_amount)
    max_amount = Decimal(settings.econfirm_max_amount)
    if amount < min_amount:
        return CustodySelection(PROVIDER_SIMULATED, f"amount_below_min_{min_amount}")
    if amount > max_amount:
        return CustodySelection(PROVIDER_SIMULATED, f"amount_above_max_{max_amount}")

    return CustodySelection(PROVIDER_ECONFIRM, "econfirm_selected")


def effective_max_principal(
    settings: _SelectionSettings, max_principal_kes: Decimal | None
) -> Decimal:
    """Enforce the lower of the Vekro-side guard and eConfirm's reported cap (decision 4)."""
    cap = Decimal(settings.econfirm_max_amount)
    if max_principal_kes is not None and Decimal(max_principal_kes) < cap:
        return Decimal(max_principal_kes)
    return cap
