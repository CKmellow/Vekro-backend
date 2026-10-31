"""RFC 6962 (Certificate Transparency) Merkle tree over anchoring leaves, with keccak256.

The leaves are already domain-separated leaf hashes (``canonical.leaf_hash``, prefix 0x00), so
the root of a single leaf is the leaf itself. Internal nodes are ``keccak256(0x01 || left ||
right)``. A tree of n leaves splits at k, the largest power of two smaller than n; the last node
of an odd level is never duplicated, which rules out the CVE-2012-2459 pattern where two
different leaf lists share a root. An empty tree has no root (the registry refuses empty batches).

Proofs are positional: each step names the sibling hash and the side it sits on, from the leaf
upwards. ``verify_inclusion`` can also check that the sides match the leaf's index in a tree of
the given size, so a proof cannot be replayed for a different position.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.anchoring.canonical import HASH_SIZE, NODE_PREFIX, keccak256

Side = Literal["left", "right"]


@dataclass(frozen=True)
class ProofStep:
    sibling: bytes
    side: Side  # where the sibling sits relative to the running hash


def node_hash(left: bytes, right: bytes) -> bytes:
    return keccak256(NODE_PREFIX, left, right)


def split_point(size: int) -> int:
    """The largest power of two strictly smaller than ``size`` (size >= 2)."""
    if size < 2:
        raise ValueError("A split needs at least two leaves.")
    return 1 << ((size - 1).bit_length() - 1)


class MerkleTree:
    def __init__(self, leaves: Sequence[bytes]) -> None:
        if not leaves:
            raise ValueError("A Merkle tree needs at least one leaf.")
        for leaf in leaves:
            if len(leaf) != HASH_SIZE:
                raise ValueError("Leaves must be 32-byte hashes.")
        self._leaves = list(leaves)
        self._roots: dict[tuple[int, int], bytes] = {}

    def __len__(self) -> int:
        return len(self._leaves)

    @property
    def root(self) -> bytes:
        return self._subtree_root(0, len(self._leaves))

    def proof(self, index: int) -> list[ProofStep]:
        if not 0 <= index < len(self._leaves):
            raise IndexError("Leaf index out of range.")
        steps: list[ProofStep] = []
        start, end = 0, len(self._leaves)
        # Walk down from the root, then reverse so the proof reads from the leaf upwards.
        while end - start > 1:
            mid = start + split_point(end - start)
            if index < mid:
                steps.append(ProofStep(self._subtree_root(mid, end), "right"))
                end = mid
            else:
                steps.append(ProofStep(self._subtree_root(start, mid), "left"))
                start = mid
        steps.reverse()
        return steps

    def _subtree_root(self, start: int, end: int) -> bytes:
        if end - start == 1:
            return self._leaves[start]
        cached = self._roots.get((start, end))
        if cached is None:
            mid = start + split_point(end - start)
            cached = node_hash(self._subtree_root(start, mid), self._subtree_root(mid, end))
            self._roots[(start, end)] = cached
        return cached


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    return MerkleTree(leaves).root


def proof_sides(index: int, size: int) -> list[Side]:
    """The sides an inclusion proof for ``index`` in a tree of ``size`` leaves must have."""
    if not 0 <= index < size:
        raise IndexError("Leaf index out of range.")
    sides: list[Side] = []
    start, end = 0, size
    while end - start > 1:
        mid = start + split_point(end - start)
        if index < mid:
            sides.append("right")
            end = mid
        else:
            sides.append("left")
            start = mid
    sides.reverse()
    return sides


def root_from_proof(leaf: bytes, proof: Sequence[ProofStep]) -> bytes:
    running = leaf
    for step in proof:
        if len(step.sibling) != HASH_SIZE:
            raise ValueError("Proof siblings must be 32-byte hashes.")
        if step.side == "left":
            running = node_hash(step.sibling, running)
        elif step.side == "right":
            running = node_hash(running, step.sibling)
        else:
            raise ValueError(f"Unknown proof side {step.side!r}.")
    return running


def verify_inclusion(
    leaf: bytes,
    proof: Sequence[ProofStep],
    root: bytes,
    *,
    index: int | None = None,
    size: int | None = None,
) -> bool:
    """True when ``proof`` takes ``leaf`` to ``root``. With ``index`` and ``size`` (the batch's
    leaf count, read from the chain), the proof must also have that position's shape."""
    if (index is None) != (size is None):
        raise ValueError("Pass both index and size, or neither.")
    try:
        if index is not None and size is not None:
            if [step.side for step in proof] != proof_sides(index, size):
                return False
        return root_from_proof(leaf, proof) == root
    except (ValueError, IndexError):
        return False
