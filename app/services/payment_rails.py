"""Which rail collects each payment method, and where the money sits (M22, docs/demo-modes.md).

The choice is made by configuration alone, so it is stated once here and printed by
``python -m app.cli payment-rails`` and at startup:

- M-Pesa: eConfirm (real third-party custody) when it is usable and the amount is inside its
  band; otherwise Daraja when ``DARAJA_ENABLED=true`` (the class-demo profile); otherwise the
  simulated stub, which is the default.
- Card: Pesapal's hosted checkout when ``PESAPAL_*`` and ``FRONTEND_URL`` are set (#251).
- USSD is not offered yet.
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
    from app.services.card_payments import card_payments_available

    if card_payments_available(settings):
        routes.append(
            RailRoute(
                "card",
                "pesapal",
                "hosted checkout",
                "the merchant's Pesapal account (collection, not custody)",
            )
        )
    else:
        routes.append(RailRoute("card", "none", "not configured", "-"))
    routes.append(RailRoute("ussd", "none", "not offered", "-"))
    return routes


def format_routes(routes: list[RailRoute]) -> str:
    return "; ".join(f"{r.method}={r.rail} ({r.when}) -> {r.money_sits}" for r in routes)


# How an order's money was collected (#257). Recorded on the transaction and anchored with its
# funding, so the UI never calls sandbox money real, or real money a demo.
RAIL_SIMULATED = "simulated"
RAIL_DARAJA = "daraja"
RAIL_PESAPAL = "pesapal"
RAIL_ECONFIRM = "econfirm"
MODE_SANDBOX = "sandbox"
MODE_LIVE = "live"


def mpesa_rail(settings: Any) -> str:
    """The rail an M-Pesa order not on eConfirm is collected on."""
    return RAIL_DARAJA if getattr(settings, "daraja_enabled", False) else RAIL_SIMULATED


def rail_mode(settings: Any, rail: str) -> str:
    """sandbox: no real money moves; live: real money is collected."""
    if rail == RAIL_DARAJA:
        return MODE_LIVE if getattr(settings, "daraja_env", "") == "production" else MODE_SANDBOX
    if rail == RAIL_PESAPAL:
        base = str(getattr(settings, "pesapal_base_url", "")).lower()
        return MODE_SANDBOX if "cybqa." in base else MODE_LIVE
    if rail == RAIL_ECONFIRM:
        base = str(getattr(settings, "econfirm_base_url", "")).lower()
        return MODE_SANDBOX if any(t in base for t in ("sandbox", "staging", "test")) else MODE_LIVE
    return MODE_SANDBOX


def set_collection(transaction: Any, settings: Any, rail: str) -> None:
    transaction.collection_rail = rail
    transaction.payment_mode = rail_mode(settings, rail)


def collection_facts(transaction: Any) -> dict[str, str]:
    """What the funding event anchors about how the money came in."""
    return {
        "collection_rail": transaction.collection_rail or RAIL_SIMULATED,
        "payment_mode": transaction.payment_mode or MODE_SANDBOX,
    }
