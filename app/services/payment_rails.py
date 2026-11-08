"""Which rail collects each payment method, and where the money sits (M22, docs/demo-modes.md).

The choice is made by configuration alone, so it is stated once here and printed by
``python -m app.cli payment-rails`` and at startup:

- M-Pesa: eConfirm (real third-party custody) when it is usable and the amount is inside its
  band; otherwise Daraja when ``DARAJA_ENABLED=true`` (the class-demo profile); otherwise the
  simulated stub, which is the default.
- Card and USSD are not offered yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.custody.econfirm_crypto import is_valid_key_config


@dataclass(frozen=True)
class RailRoute:
    method: str
    rail: str
    when: str
    money_sits: str


def _econfirm_usable(settings: Any) -> bool:
    return bool(
        settings.econfirm_enabled
        and settings.econfirm_api_key.strip()
        and is_valid_key_config(settings.econfirm_code_encryption_key)
    )


def describe_collection_rails(settings: Any) -> list[RailRoute]:
    """The routes in the order they are tried, one or more per payment method."""
    routes: list[RailRoute] = []
    if _econfirm_usable(settings):
        routes.append(
            RailRoute(
                "mpesa",
                "econfirm",
                f"KES {settings.econfirm_min_amount}-{settings.econfirm_max_amount}, "
                "breaker closed",
                "eConfirm escrow (third-party custody)",
            )
        )
    otherwise = "otherwise" if routes else "always"
    if settings.daraja_enabled:
        sandbox = settings.daraja_env == "sandbox"
        routes.append(
            RailRoute(
                "mpesa",
                "daraja",
                otherwise,
                "the shortcode's M-Pesa account"
                + (" (sandbox test shortcode)" if sandbox else " (collection, not custody)"),
            )
        )
    else:
        routes.append(RailRoute("mpesa", "simulated", otherwise, "nowhere: Vekro ledger rows only"))
    routes.append(RailRoute("card", "none", "not offered", "-"))
    routes.append(RailRoute("ussd", "none", "not offered", "-"))
    return routes


def format_routes(routes: list[RailRoute]) -> str:
    return "; ".join(f"{r.method}={r.rail} ({r.when}) -> {r.money_sits}" for r in routes)
