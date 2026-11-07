"""Verify a transaction's bundle against the AnchorRegistry (M21 A6).

The verdict is the verifier's, never the server's:

- VERIFIED: every event is intact, chained and included in a batch whose root the registry holds,
  and the live status and amount match the last anchored facts.
- PARTIALLY_PENDING: everything checked is intact, but some events are not anchored yet.
- TAMPERED: an event's hash, the chain, a proof, or the live values disagree with what was
  anchored.
- UNAVAILABLE: the registry could not be read, or there is nothing to verify.

The browser verifier (web app, docs/frontend-verification-contract.md) implements the same rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.anchoring import payloads
from app.services.anchoring.canonical import (
    ZERO_HASH,
    CanonicalEncodingError,
    canonical_json,
    leaf_hash,
)
from app.services.anchoring.chain import ChainUnavailableError
from app.services.anchoring.merkle import ProofStep, verify_inclusion

Verdict = Literal["VERIFIED", "PARTIALLY_PENDING", "TAMPERED", "UNAVAILABLE"]


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str
    event_index: int | None = None


@dataclass
class VerificationReport:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    anchored: int = 0
    pending: int = 0
    total: int = 0


RootOf = Callable[[int], bytes]


def verify_bundle(bundle: dict[str, Any], root_of: RootOf) -> VerificationReport:
    """``root_of(batch_id)`` reads the registry (zero bytes when the batch is not anchored)."""
    report = VerificationReport(verdict="VERIFIED")
    events = bundle.get("events") or []
    report.total = len(events)
    if not events:
        report.verdict = "UNAVAILABLE"
        report.findings.append(Finding("no_events", "Nothing is recorded for this transaction."))
        return report
    roots: dict[int, bytes] = {}

    def chain_root(batch_id: int) -> bytes:
        if batch_id not in roots:
            roots[batch_id] = root_of(batch_id)
        return roots[batch_id]

    try:
        prev = ZERO_HASH
        bodies: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            body, leaf = _check_event(report, index, event, prev, bundle.get("transaction_id"))
            bodies.append(body)
            _check_anchor(report, index, event.get("anchor"), leaf, chain_root)
            prev = _hex(event.get("leaf_hash"))
        _check_linked(report, bundle, bodies, chain_root)
        _check_current(report, bundle.get("current") or {}, bodies)
    except ChainUnavailableError as exc:
        report.verdict = "UNAVAILABLE"
        report.findings.append(Finding("chain_unavailable", str(exc)))
        return report

    if any(finding.code in TAMPER_CODES for finding in report.findings):
        report.verdict = "TAMPERED"
    elif report.pending:
        report.verdict = "PARTIALLY_PENDING"
    return report


TAMPER_CODES = frozenset(
    {
        "non_canonical_payload",
        "wrong_transaction",
        "chain_broken",
        "leaf_mismatch",
        "proof_invalid",
        "root_mismatch",
        "linked_missing",
        "status_mismatch",
        "amount_mismatch",
    }
)


def _check_event(
    report: VerificationReport,
    index: int,
    event: dict[str, Any],
    prev: bytes,
    transaction_id: str | None,
) -> tuple[dict[str, Any], bytes]:
    payload = str(event.get("payload", ""))
    body: dict[str, Any] = {}
    try:
        body = json.loads(payload)
        if canonical_json(body) != payload:
            report.findings.append(
                Finding("non_canonical_payload", "Payload is not canonical JSON.", index)
            )
    except (ValueError, CanonicalEncodingError):
        report.findings.append(Finding("non_canonical_payload", "Payload is not JSON.", index))
    if transaction_id is not None and body.get("transaction_id") not in (None, transaction_id):
        report.findings.append(
            Finding("wrong_transaction", "Event belongs to another transaction.", index)
        )
    if _hex(event.get("prev_event_hash")) != prev:
        report.findings.append(
            Finding("chain_broken", "Event does not follow the previous one.", index)
        )
    leaf = leaf_hash(_hex(event.get("salt")), _hex(event.get("prev_event_hash")), payload)
    if leaf != _hex(event.get("leaf_hash")):
        report.findings.append(Finding("leaf_mismatch", "Event hash does not match.", index))
    return body, leaf


def _check_anchor(
    report: VerificationReport,
    index: int,
    anchor: dict[str, Any] | None,
    leaf: bytes,
    chain_root: RootOf,
) -> None:
    state = _anchor_state(anchor, leaf, chain_root)
    if state == "anchored":
        report.anchored += 1
    elif state == "pending":
        report.pending += 1
    else:
        report.findings.append(Finding(state.code, state.detail, index))


def _anchor_state(
    anchor: dict[str, Any] | None, leaf: bytes, chain_root: RootOf
) -> Literal["anchored", "pending"] | Finding:
    if not anchor:
        return "pending"
    on_chain = chain_root(int(anchor["batch_id"]))
    if on_chain == ZERO_HASH:
        return "pending"
    proof = [ProofStep(_hex(step["sibling"]), step["side"]) for step in anchor.get("proof", [])]
    included = verify_inclusion(
        leaf, proof, on_chain, index=int(anchor["leaf_index"]), size=int(anchor["leaf_count"])
    )
    if included:
        return "anchored"
    code = "root_mismatch" if _hex(anchor.get("batch_root")) != on_chain else "proof_invalid"
    return Finding(code, f"Not included in anchored batch {anchor['batch_id']}.")


def _check_linked(
    report: VerificationReport,
    bundle: dict[str, Any],
    bodies: list[dict[str, Any]],
    chain_root: RootOf,
) -> None:
    """Linked provider events (decision D10) are extra evidence: checked, but not counted."""
    linked = {str(item.get("leaf_hash")): item for item in bundle.get("linked_events") or []}
    for index, body in enumerate(bodies):
        if body.get("type") != payloads.GLOBAL_EVENT_LINKED:
            continue
        target = linked.get(str(body.get("global_leaf_hash")))
        if target is None:
            report.findings.append(
                Finding("linked_missing", "A linked provider event is missing.", index)
            )
            continue
        leaf = leaf_hash(
            _hex(target.get("salt")), _hex(target.get("prev_event_hash")), target["payload"]
        )
        if leaf != _hex(target.get("leaf_hash")):
            report.findings.append(
                Finding("leaf_mismatch", "Linked provider event hash does not match.", index)
            )
            continue
        state = _anchor_state(target.get("anchor"), leaf, chain_root)
        if isinstance(state, Finding):
            report.findings.append(Finding(state.code, state.detail, index))


def _check_current(
    report: VerificationReport, current: dict[str, Any], bodies: list[dict[str, Any]]
) -> None:
    expected_status = None
    expected_amount = None
    for body in bodies:
        kind = body.get("type")
        if kind == payloads.TRANSACTION_TRANSITION:
            expected_status = body.get("to")
        elif kind == payloads.BACKFILL_SNAPSHOT:
            expected_status = body.get("status")
            expected_amount = body.get("amount")
        elif kind == payloads.TRANSACTION_CREATED:
            expected_amount = body.get("amount")
    if expected_status is not None and current.get("status") != expected_status:
        report.findings.append(
            Finding(
                "status_mismatch",
                f"Live status {current.get('status')!r} differs from the last recorded "
                f"{expected_status!r}.",
            )
        )
    if expected_amount is not None and current.get("amount") != expected_amount:
        report.findings.append(
            Finding("amount_mismatch", "Live amount differs from the recorded amount.")
        )


def _hex(value: Any) -> bytes:
    text = str(value or "").removeprefix("0x")
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b""
