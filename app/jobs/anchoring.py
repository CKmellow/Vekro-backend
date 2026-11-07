"""Anchoring worker (M21 A5, decision D11).

    python -m app.jobs.anchoring run-once   # one step: build, submit or confirm a batch
    python -m app.jobs.anchoring run-loop   # keep going every ANCHOR_POLL_INTERVAL_SECONDS

Refuses to run unless ANCHOR_ENABLED=true. Exit codes: 0 ok, 2 disabled or misconfigured,
3 halted (a batch disagrees with the chain; see docs/runbooks.md).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from app.core.settings import get_settings
from app.services.anchoring.batcher import AnchoringConfig, RunResult, run_once

_logger = logging.getLogger("app.anchoring.job")


def _step() -> RunResult:
    from app.db.session import _get_session_factory
    from app.services.anchoring.chain import build_registry_client

    settings = get_settings()
    client = build_registry_client(settings)
    client.check_chain()
    with _get_session_factory()() as db:
        return run_once(db, client, AnchoringConfig.from_settings(settings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.anchoring")
    parser.add_argument("mode", choices=["run-once", "run-loop"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    settings = get_settings()
    if not settings.anchor_enabled:
        print("Anchoring is disabled (ANCHOR_ENABLED=false); nothing to do.", file=sys.stderr)
        return 2

    while True:
        try:
            result = _step()
        except Exception as exc:  # keep the loop alive; the next tick retries
            _logger.exception("anchoring_step_failed error=%s", type(exc).__name__)
            result = RunResult("error", detail=type(exc).__name__)
        print(f"{result.action} batch={result.batch_id} {result.detail or ''}".rstrip())
        if result.action == "halted":
            return 3
        if args.mode == "run-once":
            return 0 if result.action != "error" else 1
        time.sleep(settings.anchor_poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
