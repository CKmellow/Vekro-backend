"""E6: failure matrix for every eConfirm operation over the real httpx transport.

Rows are network/HTTP conditions, columns are adapter operations. Each cell
asserts two independent classifications documented in
``docs/econfirm-api-observed.md`` (section 8):

* business outcome - FAILED_DEFINITE (validation rejection: no retry/failover)
  or UNKNOWN (indeterminate: GET status before any retry, never blind-retry);
* breaker health - whether the exchange counts against the circuit breaker.
"""

from __future__ import annotations

from decimal import Decimal

import httpx2 as httpx
import pytest
from app.services.custody.dto import FundingRequest, OpenEscrowRequest, PayoutRequest
from app.services.custody.econfirm_provider import EconfirmDefiniteError, EconfirmUnknownError
from app.services.custody.enums import CollectionOutcome, PayoutOutcome

from tests import econfirm_mock

DEFINITE = "definite"
UNKNOWN = "unknown"


def _raise(exc):
    def handler(request):
        raise exc

    return handler


def _respond(response_factory):
    def handler(_request):
        return response_factory()

    return handler


# (id, handler, expected business outcome, counts against breaker)
CONDITIONS = [
    ("400", _respond(lambda: econfirm_mock.error_envelope(400)), DEFINITE, False),
    ("401", _respond(lambda: econfirm_mock.error_envelope(401, "Bad key")), DEFINITE, False),
    ("404", _respond(lambda: econfirm_mock.error_envelope(404, "Not found")), DEFINITE, False),
    ("422", _respond(lambda: econfirm_mock.error_envelope(422)), DEFINITE, False),
    ("409", _respond(lambda: econfirm_mock.error_envelope(409, "In progress")), UNKNOWN, False),
    ("429", _respond(lambda: econfirm_mock.error_envelope(429, "Slow down")), UNKNOWN, True),
    ("500", _respond(lambda: econfirm_mock.error_envelope(500, "Server error")), UNKNOWN, True),
    ("502", _respond(lambda: econfirm_mock.error_envelope(502, "STK failed")), UNKNOWN, True),
    (
        "cloudflare_403",
        _respond(
            lambda: httpx.Response(
                403, json={"cloudflare_error": True, "error_code": 1010, "ray_id": "x"}
            )
        ),
        UNKNOWN,
        True,
    ),
    (
        "non_json_200",
        _respond(lambda: httpx.Response(200, text="<html>maintenance</html>")),
        UNKNOWN,
        True,
    ),
    ("timeout", _raise(httpx.ReadTimeout("read timed out")), UNKNOWN, True),
    ("connection", _raise(httpx.ConnectError("connection refused")), UNKNOWN, True),
]


def _open(provider):
    provider.open_escrow(
        OpenEscrowRequest(
            transaction_id="VK-MATRIX",
            buyer_id="b",
            seller_id="s",
            amount=Decimal("500.00"),
            receiver_phone="+254712345678",
        )
    )


def _fund(provider):
    return provider.request_funding(
        FundingRequest(
            escrow_reference="txn_M",
            amount=Decimal("500.00"),
            phone_number="+254700000001",
            account_reference="VK-MATRIX",
        )
    )


def _payout(kind):
    def run(provider):
        return getattr(provider, kind)(
            PayoutRequest(
                escrow_reference="txn_M",
                amount=Decimal("500.00"),
                destination_phone="+254700000002",
                purpose=f"matrix-{kind}",
                confirmation_code="CODE",
            )
        )

    return run


OPERATIONS = {
    "fund": (_fund, CollectionOutcome),
    "funding_status": (lambda p: p.get_funding_status("txn_M"), CollectionOutcome),
    "release": (_payout("release"), PayoutOutcome),
    "reversal": (_payout("refund"), PayoutOutcome),
    "payout_status": (lambda p: p.get_payout_status("txn_M"), PayoutOutcome),
}


@pytest.mark.parametrize(
    ("condition", "handler", "expected", "infra_failure"),
    CONDITIONS,
    ids=[c[0] for c in CONDITIONS],
)
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_operation_outcome_matrix(operation, condition, handler, expected, infra_failure):
    hook_calls = []
    provider = econfirm_mock.build_provider(handler, breaker_hook=hook_calls.append)
    run, outcome_enum = OPERATIONS[operation]

    result = run(provider)

    expected_outcome = (
        outcome_enum.FAILED_DEFINITE if expected == DEFINITE else outcome_enum.UNKNOWN
    )
    assert result.outcome == expected_outcome
    assert [call.is_success for call in hook_calls] == [not infra_failure]


@pytest.mark.parametrize(
    ("condition", "handler", "expected", "infra_failure"),
    CONDITIONS,
    ids=[c[0] for c in CONDITIONS],
)
def test_open_escrow_matrix(condition, handler, expected, infra_failure):
    # Open has no idempotency key at eConfirm: a definite rejection may fall back
    # to simulated, but UNKNOWN must go to reconciliation and never be retried.
    hook_calls = []
    provider = econfirm_mock.build_provider(handler, breaker_hook=hook_calls.append)

    expected_exc = EconfirmDefiniteError if expected == DEFINITE else EconfirmUnknownError
    with pytest.raises(expected_exc):
        _open(provider)
    assert [call.is_success for call in hook_calls] == [not infra_failure]


def test_open_success_without_id_is_unknown_not_success():
    provider = econfirm_mock.build_provider(
        _respond(lambda: econfirm_mock.envelope(201, status="pending"))
    )

    with pytest.raises(EconfirmUnknownError):
        _open(provider)


def test_indeterminate_results_are_never_cached():
    calls = []

    def handler(request):
        calls.append(request)
        return econfirm_mock.error_envelope(503, "unavailable")

    provider = econfirm_mock.build_provider(handler)
    assert _fund(provider).outcome == CollectionOutcome.UNKNOWN
    assert _fund(provider).outcome == CollectionOutcome.UNKNOWN
    assert _payout("release")(provider).outcome == PayoutOutcome.UNKNOWN
    assert _payout("release")(provider).outcome == PayoutOutcome.UNKNOWN
    # Every UNKNOWN goes back to the wire next time (status must stay pollable).
    assert len(calls) == 4
