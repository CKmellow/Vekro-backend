#!/usr/bin/env python3
"""M22 P2 discovery probe for Pesapal API 3.0 (sandbox, public demo merchant keys).

Not imported by the application. It makes only calls that move no money:

    1. POST /api/Auth/RequestToken
    2. POST /api/URLSetup/RegisterIPN       (the documented path) and, for comparison,
       POST /api/Transactions/RegisterIPN   (the path the M15 adapter used)
    3. POST /api/Transactions/SubmitOrderRequest   (one KES 1 order; nobody pays it)
    4. GET  /api/Transactions/GetTransactionStatus (status of that unpaid order)

It prints the hosted checkout URL so the page can be inspected for a card option. It never
submits payment details. Responses are sanitised (token redacted, tracking ids kept: they are
random GUIDs, not personal data) and written to tests/fixtures/pesapal/observed/.

Usage:
    .venv/bin/python scripts/pesapal_probe.py \
        --ipn-url https://<host>/api/webhooks/pesapal/callback \
        --callback-url https://<frontend>/pay/return

Reads PESAPAL_BASE_URL, PESAPAL_CONSUMER_KEY and PESAPAL_CONSUMER_SECRET from .env.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "pesapal" / "observed"


def _save(name: str, status: int, body: Any, *, hide: tuple[str, ...] = ()) -> None:
    if isinstance(body, dict) and "token" in body:
        body = {**body, "token": "<redacted>"}
    text = json.dumps(body)
    for value in hide:  # the operator's own tunnel host stays out of the repo
        text = text.replace(value, "https://tunnel.example")
    body = json.loads(text)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps({"http_status": status, "body": body}, indent=2, sort_keys=True) + "\n"
    )
    print(f"{name}: HTTP {status}")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"non_json_body_length": len(response.text)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipn-url", required=True)
    parser.add_argument("--callback-url", required=True)
    args = parser.parse_args()

    env = dotenv_values(ROOT / ".env")
    base = (env.get("PESAPAL_BASE_URL") or "").rstrip("/")
    if "cybqa.pesapal.com" not in base:
        print("Refusing: PESAPAL_BASE_URL is not the sandbox.", file=sys.stderr)
        return 2
    http = httpx.Client(base_url=base, timeout=30, headers={"Accept": "application/json"})

    auth = http.post(
        "/api/Auth/RequestToken",
        json={
            "consumer_key": env.get("PESAPAL_CONSUMER_KEY"),
            "consumer_secret": env.get("PESAPAL_CONSUMER_SECRET"),
        },
    )
    _save("auth_request_token.json", auth.status_code, _json(auth))
    token = _json(auth).get("token")
    if not token:
        return 1
    bearer = {"Authorization": f"Bearer {token}"}

    ipn_body = {"url": args.ipn_url, "ipn_notification_type": "GET"}
    old = http.post("/api/Transactions/RegisterIPN", json=ipn_body, headers=bearer)
    _save("register_ipn_m15_path.json", old.status_code, _json(old))
    ipn = http.post("/api/URLSetup/RegisterIPN", json=ipn_body, headers=bearer)
    tunnel = "/".join(args.ipn_url.split("/")[:3])
    _save("register_ipn.json", ipn.status_code, _json(ipn), hide=(tunnel,))
    ipn_id = _json(ipn).get("ipn_id")
    if not ipn_id:
        return 1

    order = http.post(
        "/api/Transactions/SubmitOrderRequest",
        headers=bearer,
        json={
            "id": f"VK-PROBE-{int(time.time())}",
            "currency": "KES",
            "amount": 1.00,
            "description": "Vekro escrow (discovery probe)",
            "callback_url": args.callback_url,
            "notification_id": ipn_id,
            "billing_address": {"email_address": "buyer@example.com", "country_code": "KE"},
        },
    )
    _save("submit_order.json", order.status_code, _json(order))
    tracking = _json(order).get("order_tracking_id")
    if not tracking:
        return 1

    status = http.get(
        "/api/Transactions/GetTransactionStatus",
        params={"orderTrackingId": tracking},
        headers=bearer,
    )
    _save("get_transaction_status_unpaid.json", status.status_code, _json(status))
    print(
        f"ipn_id={ipn_id}\norder_tracking_id={tracking}\nredirect_url={_json(order).get('redirect_url')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
