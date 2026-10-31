"""RFC 6962 Merkle tree and positional proofs over keccak256 (M21 A2)."""

import pytest
from app.services.anchoring.canonical import keccak256
from app.services.anchoring.merkle import (
    MerkleTree,
    ProofStep,
    merkle_root,
    node_hash,
    proof_sides,
    split_point,
    verify_inclusion,
)
from hypothesis import given, settings
from hypothesis import strategies as st

leaf_lists = st.lists(st.binary(min_size=32, max_size=32), min_size=1, max_size=40)


# Straight from RFC 6962 section 2.1, written independently of the implementation under test.
def _ref_root(leaves: list[bytes]) -> bytes:
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    return keccak256(b"\x01", _ref_root(leaves[:k]), _ref_root(leaves[k:]))


def _ref_path(m: int, leaves: list[bytes]) -> list[tuple[bytes, str]]:
    if len(leaves) == 1:
        return []
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    if m < k:
        return _ref_path(m, leaves[:k]) + [(_ref_root(leaves[k:]), "right")]
    return _ref_path(m - k, leaves[k:]) + [(_ref_root(leaves[:k]), "left")]


def _leaves(count: int) -> list[bytes]:
    return [keccak256(bytes([i])) for i in range(count)]


def test_split_point_is_the_largest_power_of_two_below_n() -> None:
    assert [split_point(n) for n in range(2, 10)] == [1, 2, 2, 4, 4, 4, 4, 8]
    with pytest.raises(ValueError):
        split_point(1)


def test_small_trees_have_the_rfc_6962_shape() -> None:
    a, b, c, d, e = _leaves(5)
    assert merkle_root([a]) == a
    assert merkle_root([a, b]) == node_hash(a, b)
    assert merkle_root([a, b, c]) == node_hash(node_hash(a, b), c)
    assert merkle_root([a, b, c, d, e]) == node_hash(node_hash(node_hash(a, b), node_hash(c, d)), e)


def test_last_node_is_never_duplicated() -> None:
    a, b, c = _leaves(3)
    bitcoin_style = node_hash(node_hash(a, b), node_hash(c, c))
    assert merkle_root([a, b, c]) != bitcoin_style
    # CVE-2012-2459: with duplication, [a, b, c] and [a, b, c, c] would share a root.
    assert merkle_root([a, b, c]) != merkle_root([a, b, c, c])


def test_internal_nodes_are_domain_separated_from_leaves() -> None:
    a, b = _leaves(2)
    assert node_hash(a, b) == keccak256(b"\x01" + a + b)
    assert node_hash(a, b) != keccak256(b"\x00" + a + b)


def test_empty_trees_and_bad_leaves_are_refused() -> None:
    with pytest.raises(ValueError):
        MerkleTree([])
    with pytest.raises(ValueError):
        MerkleTree([b"short"])
    with pytest.raises(IndexError):
        MerkleTree(_leaves(3)).proof(3)


@settings(max_examples=150, deadline=None)
@given(leaf_lists)
def test_matches_the_reference_and_every_proof_verifies(leaves) -> None:
    tree = MerkleTree(leaves)
    assert tree.root == _ref_root(leaves)
    for index, leaf in enumerate(leaves):
        proof = tree.proof(index)
        assert [(step.sibling, step.side) for step in proof] == _ref_path(index, leaves)
        assert [step.side for step in proof] == proof_sides(index, len(leaves))
        assert verify_inclusion(leaf, proof, tree.root, index=index, size=len(leaves))
        assert len(proof) <= (len(leaves) - 1).bit_length()


@settings(max_examples=200, deadline=None)
@given(leaves=leaf_lists, data=st.data())
def test_any_single_bit_change_fails_verification(leaves, data) -> None:
    tree = MerkleTree(leaves)
    index = data.draw(st.integers(min_value=0, max_value=len(leaves) - 1))
    leaf, proof, root = leaves[index], tree.proof(index), tree.root
    targets = ["leaf", "root"] + [f"sibling:{i}" for i in range(len(proof))]
    target = data.draw(st.sampled_from(targets))
    bit = data.draw(st.integers(min_value=0, max_value=255))

    def flip(value: bytes) -> bytes:
        changed = bytearray(value)
        changed[bit // 8] ^= 1 << (bit % 8)
        return bytes(changed)

    if target == "leaf":
        leaf = flip(leaf)
    elif target == "root":
        root = flip(root)
    else:
        position = int(target.split(":")[1])
        step = proof[position]
        proof = [
            *proof[:position],
            ProofStep(flip(step.sibling), step.side),
            *proof[position + 1 :],
        ]
    assert not verify_inclusion(leaf, proof, root, index=index, size=len(leaves))


@settings(max_examples=100, deadline=None)
@given(
    leaves=st.lists(st.binary(min_size=32, max_size=32), min_size=2, max_size=40), data=st.data()
)
def test_a_flipped_side_or_another_index_fails(leaves, data) -> None:
    tree = MerkleTree(leaves)
    index = data.draw(st.integers(min_value=0, max_value=len(leaves) - 1))
    proof = tree.proof(index)
    position = data.draw(st.integers(min_value=0, max_value=len(proof) - 1))
    step = proof[position]
    flipped = ProofStep(step.sibling, "left" if step.side == "right" else "right")
    bad = [*proof[:position], flipped, *proof[position + 1 :]]
    assert not verify_inclusion(leaves[index], bad, tree.root, index=index, size=len(leaves))

    other = data.draw(st.integers(min_value=0, max_value=len(leaves) - 1))
    if proof_sides(other, len(leaves)) != proof_sides(index, len(leaves)):
        assert not verify_inclusion(leaves[index], proof, tree.root, index=other, size=len(leaves))


def test_verify_rejects_malformed_proofs() -> None:
    leaves = _leaves(4)
    tree = MerkleTree(leaves)
    assert not verify_inclusion(leaves[0], [ProofStep(b"x", "right")], tree.root)
    assert not verify_inclusion(leaves[0], [ProofStep(leaves[1], "up")], tree.root)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        verify_inclusion(leaves[0], tree.proof(0), tree.root, index=0)
