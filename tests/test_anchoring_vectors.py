"""The committed golden vectors (tests/fixtures/anchoring/vectors.json) match the library, verify
on their own, and agree with independent implementations: Foundry's `cast keccak` and a separate
JavaScript JCS, when those tools are installed."""

import json
import shutil
import subprocess

import pytest
from app.services.anchoring.canonical import canonical_json, keccak256, leaf_hash
from app.services.anchoring.merkle import ProofStep, merkle_root, verify_inclusion

from tests.anchoring_vectors import VECTORS_PATH, render


def _vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _b(value: str) -> bytes:
    assert value.startswith("0x") and value == value.lower()
    return bytes.fromhex(value[2:])


def _proof(steps: list[dict]) -> list[ProofStep]:
    return [ProofStep(_b(step["sibling"]), step["side"]) for step in steps]


def test_committed_vectors_match_the_builder() -> None:
    assert (
        VECTORS_PATH.read_text(encoding="utf-8") == render()
    ), "vectors.json is stale: run `.venv/bin/python -m tests.anchoring_vectors`"


def test_vectors_cover_the_required_tree_sizes() -> None:
    assert [tree["size"] for tree in _vectors()["trees"]] == [1, 2, 3, 4, 5, 7, 8]


def test_every_vector_recomputes_and_verifies() -> None:
    vectors = _vectors()
    for case in vectors["keccak"]:
        assert keccak256(_b(case["input_hex"])) == _b(case["hash"])
    for case in vectors["canonical"]:
        assert canonical_json(case["value"]) == case["canonical"]

    prev = bytes(32)
    leaves = []
    for event in vectors["events"]:
        assert canonical_json(json.loads(event["payload"])) == event["payload"]
        assert _b(event["prev_event_hash"]) == prev  # one transaction's hash chain
        leaf = leaf_hash(_b(event["salt"]), prev, event["payload"])
        assert leaf == _b(event["leaf_hash"])
        leaves.append(leaf)
        prev = leaf

    for tree in vectors["trees"]:
        size = tree["size"]
        assert [_b(leaf) for leaf in tree["leaves"]] == leaves[:size]
        root = _b(tree["root"])
        assert merkle_root(leaves[:size]) == root
        for item in tree["proofs"]:
            index = item["index"]
            assert verify_inclusion(
                leaves[index], _proof(item["proof"]), root, index=index, size=size
            )

    for case in vectors["must_fail"]:
        assert not verify_inclusion(
            _b(case["leaf"]),
            _proof(case["proof"]),
            _b(case["root"]),
            index=case["index"],
            size=case["size"],
        ), case["name"]


@pytest.mark.skipif(shutil.which("cast") is None, reason="Foundry cast not installed")
def test_leaves_and_roots_agree_with_foundry_cast() -> None:
    def cast_keccak(data_hex: str) -> str:
        result = subprocess.run(
            ["cast", "keccak", data_hex], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    vectors = _vectors()
    for event in vectors["events"]:
        data = (
            "0x00"
            + event["salt"][2:]
            + event["prev_event_hash"][2:]
            + event["payload"].encode("utf-8").hex()
        )
        assert cast_keccak(data) == event["leaf_hash"]

    def cast_root(leaves: list[str]) -> str:
        if len(leaves) == 1:
            return leaves[0]
        k = 1
        while k * 2 < len(leaves):
            k *= 2
        return cast_keccak("0x01" + cast_root(leaves[:k])[2:] + cast_root(leaves[k:])[2:])

    for tree in vectors["trees"]:
        assert cast_root(tree["leaves"]) == tree["root"]


_JS_JCS = r"""
const canon = (v) =>
  v === null || typeof v !== "object"
    ? JSON.stringify(v)
    : Array.isArray(v)
      ? "[" + v.map(canon).join(",") + "]"
      : "{" +
        Object.keys(v)
          .sort() // default sort compares UTF-16 code units, as JCS requires
          .map((k) => JSON.stringify(k) + ":" + canon(v[k]))
          .join(",") +
        "}";
const vectors = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {
  canonical: vectors.canonical.map((c) => canon(c.value)),
  events: vectors.events.map((e) => canon(JSON.parse(e.payload))),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_canonical_text_agrees_with_an_independent_javascript_jcs(tmp_path) -> None:
    script = tmp_path / "jcs.js"
    script.write_text(_JS_JCS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), str(VECTORS_PATH)], capture_output=True, text=True, check=True
    )
    js = json.loads(result.stdout)
    vectors = _vectors()
    assert js["canonical"] == [case["canonical"] for case in vectors["canonical"]]
    assert js["events"] == [event["payload"] for event in vectors["events"]]
