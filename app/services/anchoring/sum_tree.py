"""Merkle sum tree for proof of liabilities (M24 B2, #259; docs/anchoring.md).

Every open escrow is a leaf that commits to an amount; every node commits to both children's
hashes and to the sum of their amounts. The root therefore commits to the total Vekro holds for
buyers, and each buyer can check, from their leaf to the root, that their amount is counted and
that no node on the way hides a negative or overflowing sum.

    leaf  = keccak256(0x00 || salt(32) || order_commit(32) || amount_cents(uint64 BE))
    node  = keccak256(0x01 || left_hash || left_sum || right_hash || right_sum), sums uint64 BE
    order_commit = keccak256(utf8("vekro-liability-order:" + lowercase transaction uuid))

``order_commit`` is recomputable by the buyer from their own order id, so a leaf cannot be shown
to two buyers. The salt (served only to the order's parties) hides the amount and the order from
everyone else. Amounts are whole cents, 0 <= sum <= 2**64 - 1; anything else is refused. The shape
is RFC 6962's (``merkle.split_point``), so no node is ever duplicated.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.services.anchoring.canonical import HASH_SIZE, LEAF_PREFIX, NODE_PREFIX, keccak256
from app.services.anchoring.merkle import Side, split_point

MAX_SUM = 2**64 - 1
_ORDER_DOMAIN = "vekro-liability-order:"


class SumTreeError(ValueError):
    """An amount or sum outside 0..2**64-1, or a malformed input."""


@dataclass(frozen=True)
class SumNode:
    hash: bytes
    sum: int


@dataclass(frozen=True)
class SumProofStep:
    sibling: SumNode
    side: Side  # where the sibling sits relative to the running node


def _u64(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SUM:
        raise SumTreeError(f"Sums must be whole numbers from 0 to 2**64-1, not {value!r}.")
    return value.to_bytes(8, "big")


def amount_cents(amount: Decimal | str) -> int:
    cents = Decimal(str(amount)) * 100
    if cents != cents.to_integral_value() or cents < 0:
        raise SumTreeError(f"Amounts must be whole, non-negative cents, not {amount}.")
    _u64(int(cents))
    return int(cents)


def order_commit(transaction_id: uuid.UUID | str) -> bytes:
    return keccak256((_ORDER_DOMAIN + str(uuid.UUID(str(transaction_id)))).encode("utf-8"))


def leaf(salt: bytes, commit: bytes, cents: int) -> SumNode:
    if len(salt) != HASH_SIZE or len(commit) != HASH_SIZE:
        raise SumTreeError("Salt and order commitment must be 32 bytes.")
    return SumNode(keccak256(LEAF_PREFIX, salt, commit, _u64(cents)), cents)


def node(left: SumNode, right: SumNode) -> SumNode:
    total = left.sum + right.sum
    if total > MAX_SUM:
        raise SumTreeError("The sum overflows 2**64-1.")
    return SumNode(
        keccak256(NODE_PREFIX, left.hash, _u64(left.sum), right.hash, _u64(right.sum)), total
    )


class SumTree:
    def __init__(self, leaves: Sequence[SumNode]) -> None:
        if not leaves:
            raise SumTreeError("A sum tree needs at least one leaf.")
        self._leaves = list(leaves)
        self._nodes: dict[tuple[int, int], SumNode] = {}

    def __len__(self) -> int:
        return len(self._leaves)

    @property
    def root(self) -> SumNode:
        return self._subtree(0, len(self._leaves))

    def proof(self, index: int) -> list[SumProofStep]:
        if not 0 <= index < len(self._leaves):
            raise IndexError("Leaf index out of range.")
        steps: list[SumProofStep] = []
        start, end = 0, len(self._leaves)
        while end - start > 1:
            mid = start + split_point(end - start)
            if index < mid:
                steps.append(SumProofStep(self._subtree(mid, end), "right"))
                end = mid
            else:
                steps.append(SumProofStep(self._subtree(start, mid), "left"))
                start = mid
        steps.reverse()
        return steps

    def _subtree(self, start: int, end: int) -> SumNode:
        if end - start == 1:
            return self._leaves[start]
        cached = self._nodes.get((start, end))
        if cached is None:
            mid = start + split_point(end - start)
            cached = node(self._subtree(start, mid), self._subtree(mid, end))
            self._nodes[(start, end)] = cached
        return cached


def root_from_proof(start: SumNode, proof: Sequence[SumProofStep]) -> SumNode:
    """Fold a proof upwards. Every sibling sum is range-checked, so a negative or overflowing
    sibling (the classic way to hide a liability) fails here, not just at the root."""
    running = start
    _u64(running.sum)
    for step in proof:
        _u64(step.sibling.sum)
        if len(step.sibling.hash) != HASH_SIZE:
            raise SumTreeError("Sibling hashes must be 32 bytes.")
        if step.side == "left":
            running = node(step.sibling, running)
        elif step.side == "right":
            running = node(running, step.sibling)
        else:
            raise SumTreeError(f"Unknown side {step.side!r}.")
    return running


def verify_sum_proof(
    *,
    salt: bytes,
    commit: bytes,
    cents: int,
    proof: Sequence[SumProofStep],
    root: SumNode,
) -> bool:
    """True when the leaf is in the tree whose root hash and total are ``root``."""
    try:
        computed = root_from_proof(leaf(salt, commit, cents), proof)
    except SumTreeError:
        return False
    return computed.hash == root.hash and computed.sum == root.sum
