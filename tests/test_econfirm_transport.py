"""E2: real httpx transport for the eConfirm adapter, driven by httpx2.MockTransport."""

from __future__ import annotations

import logging

import httpx2 as httpx
import pytest
from app.services.custody.econfirm_provider import EconfirmHttpResponse
from app.services.custody.econfirm_transport import (
    EconfirmHttpxTransport,
    EconfirmRequestOutcome,
)

URL = "https://api.econfirm.co.ke/api/v1/transactions"


def _transport(handler, hook=None, user_agent="VekroTest/1.0"):
    return EconfirmHttpxTransport(
        user_agent=user_agent,
        httpx_transport=httpx.MockTransport(handler),
        breaker_hook=hook,
    )


def _collect_hook():
    captured: list[EconfirmRequestOutcome] = []
    return captured, captured.append


def test_success_json_is_parsed_and_healthy():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(201, json={"success": True, "data": {"id": "txn_1"}})

    t = _transport(handler, hook=hook)
    resp = t("POST", URL, {"Authorization": "Bearer ek_secret"}, {"amount": 100}, 15.0)

    assert isinstance(resp, EconfirmHttpResponse)
    assert resp.status_code == 201
    assert resp.body["data"]["id"] == "txn_1"
    assert resp.non_json is False
    assert resp.cloudflare_blocked is False
    assert len(captured) == 1 and captured[0].is_success and captured[0].failure_kind is None


def test_browser_user_agent_is_sent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        seen["xrid"] = request.headers.get("x-request-id")
        return httpx.Response(200, json={"success": True})

    t = _transport(handler, user_agent="Mozilla/5.0 Chrome/124")
    t("GET", URL, {}, None, 15.0)

    assert seen["ua"] == "Mozilla/5.0 Chrome/124"
    assert seen["xrid"]  # a per-request id was attached


def test_correlation_id_is_surfaced_to_the_hook():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(200, json={"success": True})

    t = _transport(handler, hook=hook)
    t("GET", URL, {"X-Correlation-Id": "corr-123"}, None, 15.0)

    assert captured[0].correlation_id == "corr-123"


def test_4xx_validation_is_returned_but_not_an_infra_failure():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(422, json={"success": False, "message": "amount too low"})

    t = _transport(handler, hook=hook)
    resp = t("POST", URL, {}, {"amount": 1}, 15.0)

    assert resp.status_code == 422
    assert resp.body["message"] == "amount too low"
    # 4xx reached eConfirm and was answered: healthy for breaker purposes.
    assert captured[0].is_success is True
    assert captured[0].failure_kind is None


def test_5xx_is_an_infra_failure():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(502, json={"message": "payout init failed"})

    resp = _transport(handler, hook=hook)("POST", URL, {}, {"a": 1}, 15.0)

    assert resp.status_code == 502
    assert captured[0].is_success is False
    assert captured[0].failure_kind == "http_5xx"


def test_429_is_an_infra_failure():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(429, json={"message": "slow down"})

    _transport(handler, hook=hook)("GET", URL, {}, None, 15.0)
    assert captured[0].failure_kind == "http_429"


def test_timeout_maps_to_transport_error_contract():
    captured, hook = _collect_hook()

    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    resp = _transport(handler, hook=hook)("POST", URL, {}, {"a": 1}, 0.01)

    assert resp.status_code == 0
    assert resp.body["status"] == "transport_error"
    assert captured[0].is_success is False
    assert captured[0].failure_kind == "timeout"


def test_connection_error_maps_to_transport_error_contract():
    captured, hook = _collect_hook()

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    resp = _transport(handler, hook=hook)("GET", URL, {}, None, 15.0)

    assert resp.status_code == 0
    assert resp.body["status"] == "transport_error"
    assert captured[0].failure_kind == "connection"


def test_non_json_body_is_unknown_not_definite():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    resp = _transport(handler, hook=hook)("GET", URL, {}, None, 15.0)

    assert resp.non_json is True
    assert resp.body == {}
    assert captured[0].failure_kind == "non_json"


def test_cloudflare_403_is_flagged_and_counts_as_infra_failure():
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(
            403,
            json={
                "cloudflare_error": True,
                "error_code": 1010,
                "error_name": "browser_signature_banned",
            },
        )

    resp = _transport(handler, hook=hook)("GET", URL, {}, None, 15.0)

    assert resp.cloudflare_blocked is True
    assert captured[0].failure_kind == "cloudflare"
    assert captured[0].cloudflare_blocked is True


def test_cloudflare_403_detected_from_html_body():
    def handler(request):
        return httpx.Response(403, text="<html><title>Cloudflare</title>Error 1010</html>")

    resp = _transport(handler)("GET", URL, {}, None, 15.0)
    assert resp.cloudflare_blocked is True
    assert resp.non_json is True


def test_payload_is_redacted_in_logs(caplog):
    def handler(request):
        return httpx.Response(200, json={"success": True})

    t = _transport(handler)
    with caplog.at_level(logging.INFO, logger="app.custody.econfirm.transport"):
        t(
            "POST",
            URL,
            {"Authorization": "Bearer ek_sntnSECRET"},
            {"payer_phone": "254712345678", "confirmation_code": "AB12CD34"},
            15.0,
        )
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "254712345678" not in blob
    assert "AB12CD34" not in blob
    assert "ek_sntnSECRET" not in blob


def test_breaker_hook_errors_never_break_the_call():
    def handler(request):
        return httpx.Response(200, json={"success": True})

    def bad_hook(_outcome):
        raise RuntimeError("breaker down")

    resp = _transport(handler, hook=bad_hook)("GET", URL, {}, None, 15.0)
    assert resp.status_code == 200  # call still succeeded despite the hook raising


@pytest.mark.parametrize(
    "status,expected_success",
    [(200, True), (201, True), (400, True), (404, True), (409, True), (422, True), (500, False)],
)
def test_breaker_health_boundary(status, expected_success):
    captured, hook = _collect_hook()

    def handler(request):
        return httpx.Response(status, json={"ok": status})

    _transport(handler, hook=hook)("GET", URL, {}, None, 15.0)
    assert captured[0].is_success is expected_success
