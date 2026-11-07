#!/usr/bin/env python3
"""Verify one order's tamper-evident record against the AnchorRegistry (M21 A6).

    .venv/bin/python scripts/verify_transaction.py --transaction-id <uuid>
    .venv/bin/python scripts/verify_transaction.py --bundle receipt.json \
        --rpc-url http://... --registry 0x...

The bundle comes from the database (operators) or from a file a buyer or seller downloaded
(GET /transactions/{id}/verification). Batch roots are read from the registry, never taken from
the bundle. Prints VERIFIED, PARTIALLY_PENDING, TAMPERED or UNAVAILABLE with the findings.

Exit codes: 0 VERIFIED, 1 TAMPERED, 2 UNAVAILABLE, 3 PARTIALLY_PENDING.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from typing import Any

EXIT_CODES = {"VERIFIED": 0, "TAMPERED": 1, "UNAVAILABLE": 2, "PARTIALLY_PENDING": 3}


def _bundle_from_db(transaction_id: uuid.UUID) -> dict[str, Any] | None:
    from app.core.settings import get_settings
    from app.db.session import _get_session_factory
    from app.models.transaction import Transaction
    from app.services.anchoring.verification import build_transaction_verification

    with _get_session_factory()() as db:
        transaction = db.get(Transaction, transaction_id)
        if transaction is None:
            return None
        bundle = build_transaction_verification(db, transaction, get_settings())
    return json.loads(json.dumps(bundle, default=str))


def _registry_reader(rpc_url: str | None, registry: str | None) -> Callable[[int], bytes]:
    from app.core.settings import get_settings
    from app.services.anchoring.chain import Web3RegistryClient

    settings = get_settings()
    client = Web3RegistryClient(
        rpc_url=rpc_url or settings.anchor_rpc_url,
        chain_id=settings.anchor_chain_id,
        registry_address=registry or settings.anchor_registry_address,
        timeout_seconds=settings.anchor_rpc_timeout_seconds,
    )
    return client.root_of


def main(
    argv: list[str] | None = None,
    *,
    root_of: Callable[[int], bytes] | None = None,
    load_bundle: Callable[[uuid.UUID], dict[str, Any] | None] = _bundle_from_db,
) -> int:
    from app.services.anchoring.verify import verify_bundle

    parser = argparse.ArgumentParser(prog="verify_transaction.py")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--transaction-id", type=uuid.UUID)
    source.add_argument("--bundle", help="A verification JSON file.")
    parser.add_argument("--rpc-url", help="Defaults to ANCHOR_RPC_URL.")
    parser.add_argument("--registry", help="Defaults to ANCHOR_REGISTRY_ADDRESS.")
    args = parser.parse_args(argv)

    if args.bundle:
        with open(args.bundle, encoding="utf-8") as handle:
            bundle = json.load(handle)
    else:
        bundle = load_bundle(args.transaction_id)
        if bundle is None:
            print("Transaction not found.", file=sys.stderr)
            return EXIT_CODES["UNAVAILABLE"]

    try:
        reader = root_of or _registry_reader(args.rpc_url, args.registry)
    except Exception as exc:  # misconfigured registry address or RPC URL
        print(f"UNAVAILABLE: cannot read the registry ({type(exc).__name__}).")
        return EXIT_CODES["UNAVAILABLE"]
    report = verify_bundle(bundle, reader)
    print(
        f"{report.verdict} {bundle.get('reference', '')}: {report.anchored} of {report.total} "
        f"event(s) anchored, {report.pending} pending"
    )
    for finding in report.findings:
        where = f" (event {finding.event_index})" if finding.event_index is not None else ""
        print(f"  - {finding.code}{where}: {finding.detail}")
    return EXIT_CODES[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
