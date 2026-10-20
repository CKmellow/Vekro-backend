"""Shared helpers for driving the real eConfirm adapter + httpx transport offline.

The adapter is wired exactly as in production (``EconfirmCustodyProvider`` over
``EconfirmHttpxTransport``); only the network is replaced by an
``httpx2.MockTransport`` handler. Response bodies come from the sanitised E1
fixtures in ``tests/fixtures/econfirm`` wherever a real capture exists.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
from app.services.custody.econfirm_provider import EconfirmCustodyProvider
from app.services.custody.econfirm_transport import EconfirmHttpxTransport

BASE_URL = "https://api.econfirm.co.ke/api/v1"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "econfirm"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_response(name: str, **data_overrides: Any) -> httpx.Response:
    """Replay a captured response, optionally overriding fields in ``data``."""
    captured = load_fixture(name)["response"]
    body = copy.deepcopy(captured["json"])
    if data_overrides:
        body.setdefault("data", {}).update(data_overrides)
    return httpx.Response(captured["status_code"], json=body)


def envelope(status_code: int = 200, **data: Any) -> httpx.Response:
    return httpx.Response(status_code, json={"success": True, "message": "ok", "data": data})


def error_envelope(status_code: int, message: str = "rejected") -> httpx.Response:
    return httpx.Response(
        status_code, json={"success": False, "message": message, "errors": {"field": [message]}}
    )


def build_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    breaker_hook=None,
    fallback_email: str = "ops@vekro.example",
) -> EconfirmCustodyProvider:
    transport = EconfirmHttpxTransport(
        user_agent="VekroTest/1.0",
        httpx_transport=httpx.MockTransport(handler),
        breaker_hook=breaker_hook,
    )
    return EconfirmCustodyProvider(
        base_url=BASE_URL,
        api_key="ek_test_key",
        transport=transport,
        fallback_email=fallback_email,
    )


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content) if request.content else {}
