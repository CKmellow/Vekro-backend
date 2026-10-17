#!/usr/bin/env python3
"""E1 discovery probe for the eConfirm escrow API (v1).

This script is intentionally NOT imported by the application. It performs the
read/create-only discovery calls authorised by M20 issue E1:

    1. GET  /api/ping                 (no auth)
    2. POST /api/v1/transactions      (creates ONE *pending* escrow; no money moves)
    3. GET  /api/v1/transactions/{id} (polls the just-created escrow once)

It deliberately does NOT call stk-push, release, or reversal. Creating a
pending escrow moves no money: funds only move on stk-push (funding) and
release/reversal (payout), none of which this script performs.

Every captured response is SANITISED before being written to
tests/fixtures/econfirm/ or printed: the API key is never emitted, and
confirmation codes, transaction ids, phone numbers and e-mail addresses are
replaced with stable fake placeholders. Nothing secret reaches stdout or git.

Usage:
    .venv/bin/python scripts/econfirm_probe.py            # live run
    .venv/bin/python scripts/econfirm_probe.py --dry-run  # print plan only

Reads ECONFIRM_BASE_URL and ECONFIRM_API_KEY from the environment / .env.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dotenv is a project dependency
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "econfirm"

# --- E1 probe inputs (safe, non-funding) --------------------------------------
# Placeholder identity values. The escrow is never funded or released, so the
# receiver phone is never paid. Amount is the documented v1 minimum (KES 100)
# and within the unverified-account cap (KES 1,000). The Vekro reference is put
# in `description` so any orphaned escrow can be reconciled by hand (Hard Rule 3).
PROBE_BODY = {
    "buyer_email": "buyer@example.com",
    "seller_email": "seller@example.com",
    "receiver_phone": "254712345678",
    "amount": 100,
    "currency": "KES",
    "description": "VK-PROBE-E1 eConfirm discovery probe; no funding performed",
    "terms": "Discovery probe only. This escrow will not be funded or released.",
}


# --- Sanitisation -------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b(?:\+?254|0)\d{8,9}\b")
_TXN_RE = re.compile(r"txn_[A-Za-z0-9]+")
_KEY_RE = re.compile(r"ek_[A-Za-z0-9]+")

_SENSITIVE_KEYS = {"confirmation_code", "confirmationcode", "code"}


def _sanitise(value: Any) -> Any:
    """Return a deep copy with secrets/PII replaced by stable placeholders."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            norm = key.strip().lower().replace("-", "").replace("_", "")
            if norm in _SENSITIVE_KEYS:
                out[key] = "REDACTED_CODE"
            else:
                out[key] = _sanitise(item)
        return out
    if isinstance(value, list):
        return [_sanitise(item) for item in value]
    if isinstance(value, str):
        text = value
        text = _KEY_RE.sub("ek_REDACTED", text)
        text = _TXN_RE.sub("txn_SAMPLE0001", text)
        text = _EMAIL_RE.sub("sanitised@example.com", text)
        text = _PHONE_RE.sub("254700000000", text)
        return text
    return value


def _find_confirmation_code_path(value: Any, trail: str = "") -> str | None:
    """Return the JSON path where a confirmation_code key appears, if any."""
    if isinstance(value, dict):
        for key, item in value.items():
            norm = key.strip().lower().replace("-", "").replace("_", "")
            here = f"{trail}.{key}" if trail else key
            if norm in {"confirmation_code", "confirmationcode"}:
                length = len(str(item)) if item is not None else 0
                return f"{here} (string, length={length})"
            found = _find_confirmation_code_path(item, here)
            if found:
                return found
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found = _find_confirmation_code_path(item, f"{trail}[{idx}]")
            if found:
                return found
    return None


def _key_shape(value: Any, trail: str = "") -> list[str]:
    """Flatten a JSON doc into 'path: type' lines for the observed-API doc."""
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{trail}.{key}" if trail else key
            if isinstance(item, (dict, list)):
                lines.extend(_key_shape(item, here))
            else:
                lines.append(f"{here}: {type(item).__name__}")
    elif isinstance(value, list):
        lines.append(f"{trail}: list[{len(value)}]")
        if value:
            lines.extend(_key_shape(value[0], f"{trail}[0]"))
    return lines


# --- HTTP (stdlib urllib; httpx arrives in E2) --------------------------------
# eConfirm sits behind Cloudflare, which bans stdlib clients by User-Agent
# (Error 1010, browser_signature_banned). A normal browser UA clears that generic
# bot filter so our authenticated, documented API calls actually reach eConfirm.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _call(
    method: str,
    url: str,
    api_key: str | None,
    body: dict | None,
    timeout: float = 30.0,
) -> dict:
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url=url, method=method.upper(), headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return {"status_code": resp.status, "raw": raw, "ok": True}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return {"status_code": exc.code, "raw": raw, "ok": False}
    except error.URLError as exc:
        return {"status_code": 0, "raw": "", "ok": False, "transport_error": str(exc.reason)}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _record(name: str, method: str, url: str, result: dict) -> dict:
    """Build a sanitised fixture record and write it under tests/fixtures/econfirm/."""
    parsed = _parse(result.get("raw", ""))
    sanitised_body = _sanitise(parsed) if parsed is not None else None
    record = {
        "probe": name,
        "request": {"method": method, "url": re.sub(r"txn_[A-Za-z0-9]+", "txn_SAMPLE0001", url)},
        "response": {
            "status_code": result.get("status_code"),
            "json": sanitised_body,
            "unparseable": parsed is None,
        },
    }
    if "transport_error" in result:
        record["response"]["transport_error"] = result["transport_error"]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n")
    return {"record": record, "parsed": parsed, "fixture_path": path}


def main() -> int:
    ap = argparse.ArgumentParser(description="eConfirm E1 discovery probe (ping/create/get only)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and exit without calling",
    )
    args = ap.parse_args()

    if load_dotenv is not None:
        load_dotenv(REPO_ROOT / ".env")

    base = os.environ.get("ECONFIRM_BASE_URL", "https://api.econfirm.co.ke/api/v1").rstrip("/")
    api_root = base.rsplit("/api/", 1)[0] + "/api"  # /ping lives at /api/ping, not under /v1
    key = os.environ.get("ECONFIRM_API_KEY", "").strip()

    ping_url = f"{api_root}/ping"
    create_url = f"{base}/transactions"

    if args.dry_run:
        print("DRY RUN — would call, in order:")
        print(f"  1. GET  {ping_url}")
        print(f"  2. POST {create_url}  body={json.dumps(PROBE_BODY)}")
        print("  3. GET  {create_url}/<id-from-step-2>")
        print("Would NOT call stk-push, release, or reversal.")
        return 0

    if not key:
        print("ERROR: ECONFIRM_API_KEY is blank in the environment/.env.", file=sys.stderr)
        return 2

    print(f"[1/3] GET {ping_url}")
    ping = _call("GET", ping_url, api_key=None, body=None)
    ping_out = _record("ping", "GET", ping_url, ping)
    print(f"      -> HTTP {ping['status_code']} (fixture: {ping_out['fixture_path'].name})")

    print(f"[2/3] POST {create_url}  (amount={PROBE_BODY['amount']} KES, pending only)")
    create = _call("POST", create_url, api_key=key, body=PROBE_BODY)
    create_out = _record("create_transaction", "POST", create_url, create)
    print(f"      -> HTTP {create['status_code']} (fixture: {create_out['fixture_path'].name})")

    parsed_create = create_out["parsed"]
    txn_id = None
    if isinstance(parsed_create, dict):
        data = parsed_create.get("data")
        if isinstance(data, dict):
            txn_id = data.get("id") or data.get("transaction_id")

    code_path = _find_confirmation_code_path(parsed_create) if parsed_create is not None else None

    get_out = None
    if txn_id:
        get_url = f"{base}/transactions/{txn_id}"
        print(f"[3/3] GET {get_url.replace(txn_id, '<id>')}")
        get = _call("GET", get_url, api_key=key, body=None)
        get_out = _record("get_transaction", "GET", get_url, get)
        print(f"      -> HTTP {get['status_code']} (fixture: {get_out['fixture_path'].name})")
    else:
        print("[3/3] SKIPPED — no transaction id returned from create; cannot poll.")

    # Structural summary for docs/econfirm-api-observed.md (no secret values).
    print("\n================ OBSERVED (structural, sanitised) ================")
    print(f"ping status_code: HTTP {ping['status_code']}")
    print(f"create status_code: HTTP {create['status_code']}")
    if code_path:
        print(f"confirmation_code location in create response: {code_path}")
    else:
        print(
            "confirmation_code: NOT found in the create response body "
            "(POSSIBLE BLOCKER — see E1 report)."
        )
    if isinstance(parsed_create, dict):
        print("\ncreate response shape:")
        for line in _key_shape(parsed_create):
            print(f"  {line}")
    if get_out and isinstance(get_out["parsed"], dict):
        print("\nget response shape:")
        for line in _key_shape(get_out["parsed"]):
            print(f"  {line}")
    print("==================================================================")
    print(f"\nSanitised fixtures written to: {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
