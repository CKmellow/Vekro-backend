"""Golden vectors for anchoring (M21 A2), shared with the frontend verifier.

Regenerate with `.venv/bin/python -m tests.anchoring_vectors` after a deliberate format change;
tests/test_anchoring_vectors.py fails whenever the committed file and this builder disagree.
Everything is deterministic: salts are keccak256("vekro-vector-salt-<i>"), never random.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.anchoring.canonical import (
    ZERO_HASH,
    canonical_json,
    hex32,
    keccak256,
    leaf_hash,
    money,
    ratio,
    text_keccak,
)
from app.services.anchoring.merkle import MerkleTree, ProofStep, root_from_proof

VECTORS_PATH = Path(__file__).parent / "fixtures" / "anchoring" / "vectors.json"
TREE_SIZES = (1, 2, 3, 4, 5, 7, 8)

TXN = "5b0e6c1e-1f6a-4b8e-9c2d-3f4a5b6c7d8e"
ESCROW = "0d9c8b7a-6f5e-4d3c-2b1a-0f9e8d7c6b5a"
BUYER = "11111111-2222-4333-8444-555555555555"
SELLER = "66666666-7777-4888-9999-aaaaaaaaaaaa"
DISPUTE = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
# An HMAC admin ref (decision D5); the vector only needs a fixed 64-hex value.
ADMIN_REF = keccak256(b"vekro-vector-admin").hex()


def _payloads() -> list[dict[str, Any]]:
    """Eight events in one transaction's life, in the schema of docs/anchoring-design.md 4."""
    common = {"v": 1, "transaction_id": TXN}
    return [
        {
            **common,
            "type": "transaction_created",
            "at": "2026-10-31T09:00:00Z",
            "reference": "VK-7K3P9Q",
            "listing_id": "22222222-3333-4444-8555-666666666666",
            "buyer_id": BUYER,
            "seller_id": SELLER,
            "amount": money("500.00"),
            "currency": "KES",
        },
        {
            **common,
            "type": "transaction_transition",
            "at": "2026-10-31T09:01:30Z",
            "from": "awaiting_payment",
            "to": "locked",
            "actor_type": "provider",
            "actor_id": None,
            "actor_ref": None,
            "reason": "payment_callback_confirmed",
            "door_rejection_reason": None,
        },
        {
            **common,
            "type": "ledger_entry",
            "at": "2026-10-31T09:01:30Z",
            "ledger_entry_id": "33333333-4444-4555-8666-777777777777",
            "escrow_id": ESCROW,
            "entry_group_id": "44444444-5555-4666-8777-888888888888",
            "idempotency_key": f"callback-funding:{TXN}",
            "account_code": "ESCROW_HELD",
            "side": "debit",
            "amount": money("500.00"),
            "currency": "KES",
            "description": "Simulated custody funding (payment callback)",
        },
        {
            **common,
            "type": "dispute_opened",
            "at": "2026-10-31T10:00:00Z",
            "dispute_id": DISPUTE,
            "actor_type": "user",
            "actor_id": BUYER,
            "actor_ref": None,
            "dispute_type": "functional",
            "category": "not_working",
            "route": "seller_resolution",
            "description_keccak": text_keccak("Screen stays black — «тест» 📱"),
        },
        {
            **common,
            "type": "dispute_escalated",
            "at": "2026-10-31T11:00:00Z",
            "dispute_id": DISPUTE,
            "actor_type": "user",
            "actor_id": SELLER,
            "actor_ref": None,
            "by": "seller",
            "reason": "party_escalated",
            "reason_text_keccak": text_keccak('Buyer says "broken"\nI disagree.'),
        },
        {
            **common,
            "type": "admin_decision",
            "at": "2026-10-31T12:00:00Z",
            "dispute_id": DISPUTE,
            "decision": "split",
            "reason": "Evidence supports a partial refund.\tBoth parties agreed.",
            "admin_ref": ADMIN_REF,
            "seller_share": ratio("0.6000"),
            "release_amount": money("300.00"),
            "refund_amount": money("200.00"),
        },
        {
            **common,
            "type": "money_audit",
            "at": "2026-10-31T12:00:00Z",
            "audit_event_id": "55555555-6666-4777-8888-999999999999",
            "action": "payout_release_queued",
            "actor_type": "admin",
            "actor_id": None,
            "actor_ref": ADMIN_REF,
            "reason": "Evidence supports a partial refund.",
            "escrow_id": ESCROW,
            "attempt_id": "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
            "provider_reference": None,
            "rail_name": "simulated",
            "correlation_id": f"txn={TXN}|ref=-|rail=simulated",
            "amount": money("300.00"),
            "currency": "KES",
            "details": {"purpose": f"admin-split-release:{DISPUTE}"},
        },
        {
            **common,
            "type": "transaction_transition",
            "at": "2026-10-31T12:00:00Z",
            "from": "escalated_admin_review",
            "to": "resolved_split",
            "actor_type": "admin",
            "actor_id": None,
            "actor_ref": ADMIN_REF,
            "reason": "admin_decision",
            "door_rejection_reason": None,
        },
    ]


def _canonical_cases() -> list[dict[str, Any]]:
    cases: list[tuple[str, Any]] = [
        ("key order", {"b": 1, "a": 2, "A": 3, "aa": 4, "_": 5}),
        ("utf-16 key order (not code point order)", {"￿": 1, "\U0001f600": 2, "é": 3}),
        ("escapes", {"s": 'quote " backslash \\ tab \t newline \n cr \r bs \b ff \f'}),
        ("control characters as lowercase \\u00xx", {"s": "\x00\x01\x1f\x7f"}),
        ("non-ascii stays literal", {"s": "é 😀     «тест»"}),
        ("nested and empty", {"z": [], "y": {}, "x": [{"b": None, "a": [True, False]}]}),
        ("integers", {"n": [0, -1, 9007199254740991, -9007199254740991]}),
        ("money is a string", {"amount": money("1500"), "ratio": ratio("0.5")}),
    ]
    return [
        {"name": name, "value": value, "canonical": canonical_json(value)} for name, value in cases
    ]


def _proof_json(proof: list[ProofStep]) -> list[dict[str, str]]:
    return [{"sibling": hex32(step.sibling), "side": step.side} for step in proof]


def build_vectors() -> dict[str, Any]:
    events = []
    prev = ZERO_HASH
    leaves: list[bytes] = []
    for index, payload in enumerate(_payloads()):
        salt = keccak256(f"vekro-vector-salt-{index}".encode())
        text = canonical_json(payload)
        leaf = leaf_hash(salt, prev, text)
        events.append(
            {
                "index": index,
                "event_type": payload["type"],
                "payload": text,
                "salt": hex32(salt),
                "prev_event_hash": hex32(prev),
                "leaf_hash": hex32(leaf),
            }
        )
        leaves.append(leaf)
        prev = leaf

    trees = []
    for size in TREE_SIZES:
        tree = MerkleTree(leaves[:size])
        trees.append(
            {
                "size": size,
                "leaves": [hex32(leaf) for leaf in leaves[:size]],
                "root": hex32(tree.root),
                "proofs": [
                    {"index": index, "proof": _proof_json(tree.proof(index))}
                    for index in range(size)
                ],
            }
        )

    # Cases a verifier must reject, all against the size-5 tree.
    tree5 = MerkleTree(leaves[:5])
    good = tree5.proof(2)
    tampered_payload = events[2]["payload"].replace('"500.00"', '"5000.00"')
    tampered_leaf = leaf_hash(
        bytes.fromhex(events[2]["salt"][2:]),
        bytes.fromhex(events[2]["prev_event_hash"][2:]),
        tampered_payload,
    )
    flipped = [
        ProofStep(good[0].sibling, "left" if good[0].side == "right" else "right"),
        *good[1:],
    ]
    negatives = [
        {
            "name": "payload amount edited",
            "leaf": hex32(tampered_leaf),
            "payload": tampered_payload,
            "index": 2,
            "size": 5,
            "proof": _proof_json(good),
            "root": hex32(tree5.root),
        },
        {
            "name": "proof side flipped",
            "leaf": hex32(leaves[2]),
            "index": 2,
            "size": 5,
            "proof": _proof_json(flipped),
            "root": hex32(tree5.root),
        },
        {
            "name": "valid proof claimed for another index",
            "leaf": hex32(leaves[2]),
            "index": 3,
            "size": 5,
            "proof": _proof_json(good),
            "root": hex32(tree5.root),
        },
        {
            "name": "duplicated last leaf (CVE-2012-2459 shape) gives a different root",
            "leaf": hex32(leaves[2]),
            "index": 2,
            "size": 4,
            "proof": _proof_json(MerkleTree([*leaves[:3], leaves[2]]).proof(2)),
            "root": hex32(MerkleTree(leaves[:3]).root),
        },
    ]
    assert root_from_proof(leaves[2], good) == tree5.root

    return {
        "description": (
            "Vekro M21 anchoring golden vectors. Bytes are 0x-prefixed lowercase hex. "
            "See docs/anchoring-design.md and docs/frontend-verification-contract.md."
        ),
        "version": 1,
        "algorithms": {
            "hash": "keccak256 (Ethereum, not SHA3-256)",
            "payload": "RFC 8785 JSON Canonicalization Scheme; no floats; money as 2-place strings",
            "leaf": "keccak256(0x00 || salt[32] || prev_event_hash[32] || utf8(payload))",
            "node": "keccak256(0x01 || left[32] || right[32])",
            "tree": (
                "RFC 6962: root(1 leaf) = the leaf; otherwise split at k = largest power of two "
                "< n; root = node(root(first k), root(rest)); the last node is never duplicated"
            ),
            "proof": (
                "steps from the leaf upwards; side = where the sibling sits; "
                "left: h = node(sibling, h); right: h = node(h, sibling). The sides must also "
                "match the leaf index for a tree of the on-chain leaf count."
            ),
        },
        "keccak": [
            {"input_hex": "0x", "hash": hex32(keccak256(b""))},
            {"input_hex": "0x" + b"vekro".hex(), "hash": hex32(keccak256(b"vekro"))},
        ],
        "canonical": _canonical_cases(),
        "events": events,
        "trees": trees,
        "must_fail": negatives,
    }


def render() -> str:
    return json.dumps(build_vectors(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTORS_PATH.write_text(render(), encoding="utf-8")
    print(f"Wrote {VECTORS_PATH}")
