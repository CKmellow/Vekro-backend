"""Startup validation for the anchoring chain settings (M21 A5).

Kept free of web3 imports so settings validation stays cheap. The private key is checked for
shape only and never echoed back in a message.
"""

from __future__ import annotations

import re
from typing import Any

ANVIL_CHAIN_ID = 31337
BASE_SEPOLIA_CHAIN_ID = 84532
BASE_MAINNET_CHAIN_ID = 8453
SUPPORTED_CHAINS = {
    ANVIL_CHAIN_ID: "anvil",
    BASE_SEPOLIA_CHAIN_ID: "base-sepolia",
    BASE_MAINNET_CHAIN_ID: "base",
}

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PRIVATE_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def anchor_config_violations(settings: Any, *, environment: str) -> list[str]:
    violations: list[str] = []
    if not str(settings.anchor_rpc_url).strip():
        violations.append("ANCHOR_RPC_URL is required when ANCHOR_ENABLED=true")
    if settings.anchor_chain_id not in SUPPORTED_CHAINS:
        violations.append(
            "ANCHOR_CHAIN_ID must be one of "
            + ", ".join(f"{cid} ({name})" for cid, name in SUPPORTED_CHAINS.items())
        )
    elif environment == "production" and settings.anchor_chain_id == ANVIL_CHAIN_ID:
        violations.append("ANCHOR_CHAIN_ID 31337 (Anvil) is not allowed in production")
    if not _ADDRESS.match(str(settings.anchor_registry_address)):
        violations.append("ANCHOR_REGISTRY_ADDRESS must be a 0x-prefixed 20-byte address")
    if not _PRIVATE_KEY.match(secret_value(settings.anchor_private_key)):
        violations.append("ANCHOR_PRIVATE_KEY must be a 32-byte hex key (value not shown)")
    if settings.anchor_max_batch_size < 1:
        violations.append("ANCHOR_MAX_BATCH_SIZE must be at least 1")
    if settings.anchor_min_confirmations < 1:
        violations.append("ANCHOR_MIN_CONFIRMATIONS must be at least 1")
    return violations


def secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else (value or ""))
