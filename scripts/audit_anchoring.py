#!/usr/bin/env python3
"""Full anchoring audit from the source tables (M21 A6). Operators and admins only.

    .venv/bin/python scripts/audit_anchoring.py            # with on-chain roots
    .venv/bin/python scripts/audit_anchoring.py --no-chain # database checks only

Checks every batch (count, gapless seq, root, links, on-chain root), every hash chain, every
source row against its anchored payload, and the live status, amounts, admin decisions and escrow
totals against the last anchored facts. See app/services/anchoring/full_audit.py.

Exit codes: 0 CLEAN, 1 TAMPERED, 2 UNAVAILABLE.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from sqlalchemy.orm import Session

EXIT_CODES = {"CLEAN": 0, "TAMPERED": 1, "UNAVAILABLE": 2}


def _session() -> Session:
    from app.db.session import _get_session_factory

    return _get_session_factory()()


def main(
    argv: list[str] | None = None,
    *,
    session: Callable[[], Session] = _session,
    root_of: Callable[[int], bytes] | None = None,
) -> int:
    from app.services.anchoring.full_audit import run_full_audit

    parser = argparse.ArgumentParser(prog="audit_anchoring.py")
    parser.add_argument("--no-chain", action="store_true", help="Skip the on-chain root check.")
    args = parser.parse_args(argv)

    reader = root_of
    if reader is None and not args.no_chain:
        from app.core.settings import get_settings
        from app.services.anchoring.chain import build_registry_client

        reader = build_registry_client(get_settings(), with_key=False).root_of

    with session() as db:
        report = run_full_audit(db, None if args.no_chain else reader)
    print(f"{report.verdict}: {report.events} event(s) in {report.batches} batch(es)")
    for finding in report.findings:
        where = f" [transaction {finding.transaction_id}]" if finding.transaction_id else ""
        print(f"  - {finding.code}{where}: {finding.detail}")
    return EXIT_CODES[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
