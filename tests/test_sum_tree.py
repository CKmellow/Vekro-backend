"""#259: the liabilities sum tree, against the golden vectors the frontend verifies too."""

from __future__ import annotations

import json
import os

import pytest
from app.services.anchoring.sum_tree import (
    MAX_SUM,
    SumNode,
    SumProofStep,
    SumTree,
    SumTreeError,
    amount_cents,
    leaf,
    node,
    order_commit,
    verify_sum_proof,
)

from tests.sum_tree_vectors import VECTORS_PATH, build

VECTORS = json.loads(VECTORS_PATH.read_text())


def _b(hex_text: str) -> bytes:
    return bytes.fromhex(hex_text.removeprefix("0x"))


def _proof(steps: list[dict[str, str]]) -> list[SumProofStep]:
    return [
        SumProofStep(SumNode(_b(s["sibling_hash"]), int(s["sibling_sum"])), s["side"])  # type: ignore[arg-type]
        for s in steps
    ]


def _verify(case_leaf: dict[str, str], steps, root: dict[str, str]) -> bool:
    return verify_sum_proof(
        salt=_b(case_leaf["salt"]),
        commit=_b(case_leaf["order_commit"]),
        cents=int(case_leaf["amount_cents"]),
        proof=_proof(steps),
        root=SumNode(_b(root["hash"]), int(root["sum"])),
    )


def test_the_committed_vectors_match_the_builder() -> None:
    assert VECTORS == build(), "Regenerate with python -m tests.sum_tree_vectors"


@pytest.mark.parametrize("tree", VECTORS["trees"], ids=lambda t: f"size-{t['size']}")
def test_every_leaf_proves_into_its_root(tree) -> None:
    assert int(tree["root"]["sum"]) == sum(int(x["amount_cents"]) for x in tree["leaves"])
    for case_leaf, steps in zip(tree["leaves"], tree["proofs"], strict=True):
        assert _verify(case_leaf, steps, tree["root"])


@pytest.mark.parametrize("case", VECTORS["must_fail"], ids=lambda c: c["name"].split(":")[0])
def test_must_fail(case) -> None:
    assert not _verify(case["leaf"], case["proof"], case["root"])


def test_order_commitments_are_recomputable_from_the_order_id() -> None:
    for item in VECTORS["order_commit"]:
        assert "0x" + order_commit(item["transaction_id"]).hex() == item["order_commit"]
    assert order_commit(item["transaction_id"].upper()).hex() == item["order_commit"][2:]


def test_amounts_and_sums_are_range_checked() -> None:
    assert amount_cents("6500.00") == 650000
    for bad in ("1.005", "-1"):
        with pytest.raises(SumTreeError):
            amount_cents(bad)
    salt, commit = os.urandom(32), os.urandom(32)
    with pytest.raises(SumTreeError):
        leaf(salt, commit, MAX_SUM + 1)
    with pytest.raises(SumTreeError):
        node(leaf(salt, commit, MAX_SUM), leaf(salt, commit, 1))
    with pytest.raises(SumTreeError):
        SumTree([])
