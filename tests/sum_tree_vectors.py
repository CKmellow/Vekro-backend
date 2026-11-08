"""Golden vectors for the liabilities sum tree (M24 B2, #259), shared with the frontend verifier.

Regenerate with `.venv/bin/python -m tests.sum_tree_vectors` after a deliberate format change;
tests/test_sum_tree.py fails whenever the committed file and this builder disagree. Salts are
keccak256("vekro-sum-salt-<i>"), never random. Sums and amounts are decimal strings: they can
exceed JavaScript's safe integers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.anchoring.canonical import hex32, keccak256
from app.services.anchoring.sum_tree import (
    MAX_SUM,
    SumNode,
    SumProofStep,
    SumTree,
    leaf,
    order_commit,
)

VECTORS_PATH = Path(__file__).parent / "fixtures" / "anchoring" / "sum_tree_vectors.json"
ORDERS = [
    "5b0e6c1e-1f6a-4b8e-9c2d-3f4a5b6c7d8e",
    "0d9c8b7a-6f5e-4d3c-2b1a-0f9e8d7c6b5a",
    "11111111-2222-4333-8444-555555555555",
    "66666666-7777-4888-8999-aaaaaaaaaaaa",
    "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
    "12345678-9abc-4def-8123-456789abcdef",
    "fedcba98-7654-4321-8fed-cba987654321",
]
AMOUNTS = [650000, 1, 2400000, 0, 99999999, 320000, 5200000]  # cents
SIZES = (1, 2, 3, 4, 5, 7)


def _salt(i: int) -> bytes:
    return keccak256(f"vekro-sum-salt-{i}".encode())


def _leaves(n: int) -> list[tuple[bytes, bytes, int, SumNode]]:
    rows = []
    for i in range(n):
        salt, commit = _salt(i), order_commit(ORDERS[i])
        rows.append((salt, commit, AMOUNTS[i], leaf(salt, commit, AMOUNTS[i])))
    return rows


def _node(n: SumNode) -> dict[str, str]:
    return {"hash": hex32(n.hash), "sum": str(n.sum)}


def _proof(steps: list[SumProofStep]) -> list[dict[str, str]]:
    return [
        {"sibling_hash": hex32(s.sibling.hash), "sibling_sum": str(s.sibling.sum), "side": s.side}
        for s in steps
    ]


def _leaf_json(salt: bytes, commit: bytes, cents: int | str) -> dict[str, str]:
    return {"salt": hex32(salt), "order_commit": hex32(commit), "amount_cents": str(cents)}


def build() -> dict[str, Any]:
    trees = []
    for n in SIZES:
        rows = _leaves(n)
        tree = SumTree([r[3] for r in rows])
        trees.append(
            {
                "size": n,
                "leaves": [
                    {**_leaf_json(salt, commit, cents), "hash": hex32(node.hash)}
                    for salt, commit, cents, node in rows
                ],
                "root": _node(tree.root),
                "proofs": [_proof(tree.proof(i)) for i in range(n)],
            }
        )

    rows4 = _leaves(4)
    tree4 = SumTree([r[3] for r in rows4])
    tree3 = SumTree([r[3] for r in rows4[:3]])
    salt1, commit1, cents1, _ = rows4[1]
    salt3, commit3, cents3, _ = rows4[3]
    proof1 = _proof(tree4.proof(1))

    def bent(index: int, **change: str) -> list[dict[str, str]]:
        steps = [dict(step) for step in proof1]
        steps[index].update(change)
        return steps

    must_fail = [
        {
            "name": "tampered amount: the buyer's amount edited after the snapshot",
            "leaf": _leaf_json(salt1, commit1, cents1 + 1),
            "proof": proof1,
            "root": _node(tree4.root),
        },
        {
            "name": "omitted escrow: the published total was built without this order",
            "leaf": _leaf_json(salt3, commit3, cents3),
            "proof": _proof(tree4.proof(3)),
            "root": _node(tree3.root),
        },
        {
            "name": "understated total: the right root hash with a smaller total",
            "leaf": _leaf_json(salt1, commit1, cents1),
            "proof": proof1,
            "root": {"hash": hex32(tree4.root.hash), "sum": str(tree4.root.sum - 1)},
        },
        {
            "name": "negative sibling sum: a hidden negative liability",
            "leaf": _leaf_json(salt1, commit1, cents1),
            "proof": bent(0, sibling_sum="-650000"),
            "root": _node(tree4.root),
        },
        {
            "name": "overflowing sibling sum: wraps the total around 2**64",
            "leaf": _leaf_json(salt1, commit1, cents1),
            "proof": bent(0, sibling_sum=str(MAX_SUM + 1)),
            "root": _node(tree4.root),
        },
        {
            "name": "overflowing node: two in-range sums whose total exceeds 2**64-1",
            "leaf": _leaf_json(salt1, commit1, cents1),
            "proof": bent(0, sibling_sum=str(MAX_SUM)),
            "root": _node(tree4.root),
        },
        {
            "name": "wrong side: the sibling placed on the other side",
            "leaf": _leaf_json(salt1, commit1, cents1),
            "proof": bent(0, side="right" if proof1[0]["side"] == "left" else "left"),
            "root": _node(tree4.root),
        },
        {
            "name": "negative amount in the buyer's own leaf",
            "leaf": _leaf_json(salt1, commit1, "-1"),
            "proof": proof1,
            "root": _node(tree4.root),
        },
    ]
    return {
        "description": (
            "Vekro liabilities Merkle sum tree (backend #259, docs/anchoring.md). Byte-identical "
            "copy in the frontend. Regenerate with python -m tests.sum_tree_vectors."
        ),
        "version": 1,
        "algorithms": {
            "order_commit": 'keccak256(utf8("vekro-liability-order:" + lowercase uuid))',
            "leaf": "keccak256(0x00 || salt || order_commit || uint64_be(amount_cents))",
            "node": "keccak256(0x01 || left.hash || uint64_be(left.sum) || right.hash || "
            "uint64_be(right.sum)); sum = left.sum + right.sum",
            "range": "every amount and sum is a whole number in 0..2**64-1, else invalid",
            "shape": "RFC 6962: split at the largest power of two smaller than n",
            "proof": "from the leaf up; side is where the sibling sits",
        },
        "order_commit": [
            {"transaction_id": tid, "order_commit": hex32(order_commit(tid))} for tid in ORDERS[:2]
        ],
        "trees": trees,
        "must_fail": must_fail,
    }


def write() -> None:
    VECTORS_PATH.write_text(json.dumps(build(), indent=2) + "\n")


if __name__ == "__main__":
    write()
    print(f"Wrote {VECTORS_PATH}")
