"""An in-memory AnchorRegistry and chain for the batcher tests (M21 A5).

It applies the contract's own checks (contracts/src/AnchorRegistry.sol), keeps a mempool keyed by
nonce (a replacement at the same nonce wins only with a higher fee), mines on demand, and can
snapshot and revert like Anvil's evm_snapshot/evm_revert. ``down`` makes every call fail like an
unreachable RPC.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from app.services.anchoring.canonical import ZERO_HASH, keccak256
from app.services.anchoring.chain import (
    AnchorCall,
    ChainUnavailableError,
    OnChainBatch,
    SignedAnchorTx,
    TxReceipt,
)


@dataclass
class _Tx:
    tx_hash: bytes
    call: AnchorCall
    nonce: int
    max_fee: int
    priority_fee: int


@dataclass
class _State:
    batches: dict[int, OnChainBatch] = field(default_factory=dict)
    last_root: bytes = ZERO_HASH
    last_seq: int = 0
    block: int = 1
    nonce: int = 0
    mempool: dict[int, _Tx] = field(default_factory=dict)
    receipts: dict[bytes, TxReceipt] = field(default_factory=dict)
    block_hashes: dict[int, bytes] = field(default_factory=dict)


class FakeRegistry:
    chain_id = 31337
    registry_address = "0x" + "11" * 20
    anchorer_address = "0x" + "22" * 20

    def __init__(self, *, base_fee: int = 1_000_000_000, priority_fee: int = 100_000_000) -> None:
        self.state = _State()
        self.down = False
        self.base_fee = base_fee
        self.priority = priority_fee
        self.signed: dict[bytes, _Tx] = {}
        self.broadcasts: list[bytes] = []

    # --- test controls -------------------------------------------------------------------
    def _check(self) -> None:
        if self.down:
            raise ChainUnavailableError("ConnectionError: RPC unreachable")

    def mine(self, blocks: int = 1) -> None:
        for _ in range(blocks):
            tx = self.state.mempool.pop(self.state.nonce, None)
            self.state.block += 1
            self.state.block_hashes[self.state.block] = keccak256(
                b"block", self.state.block.to_bytes(8, "big"), str(len(self.signed)).encode()
            )
            if tx is None:
                continue
            ok = self._apply(tx.call)
            self.state.receipts[tx.tx_hash] = TxReceipt(
                tx_hash=tx.tx_hash,
                succeeded=ok,
                block_number=self.state.block,
                block_hash=self.state.block_hashes[self.state.block],
            )
            self.state.nonce += 1

    def snapshot(self) -> _State:
        return copy.deepcopy(self.state)

    def revert(self, snapshot: _State) -> None:
        self.state = copy.deepcopy(snapshot)

    def anchor_directly(self, call: AnchorCall) -> bool:
        """Anchor without a transaction (someone else holding the key, in a divergence test)."""
        ok = self._apply(call)
        self.state.block += 1
        return ok

    def _apply(self, call: AnchorCall) -> bool:
        state = self.state
        if (
            call.batch_id != len(state.batches) + 1
            or call.prev_root != state.last_root
            or call.root == ZERO_HASH
            or call.leaf_count == 0
            or call.first_seq != state.last_seq + 1
            or call.last_seq - call.first_seq + 1 != call.leaf_count
            or any(batch.root == call.root for batch in state.batches.values())
        ):
            return False
        state.batches[call.batch_id] = OnChainBatch(
            batch_id=call.batch_id,
            root=call.root,
            prev_root=call.prev_root,
            first_seq=call.first_seq,
            last_seq=call.last_seq,
            leaf_count=call.leaf_count,
            anchored_at=1_700_000_000 + state.block,
            block_number=state.block,
        )
        state.last_root = call.root
        state.last_seq = call.last_seq
        return True

    # --- RegistryClient --------------------------------------------------------------------
    def batch_count(self) -> int:
        self._check()
        return len(self.state.batches)

    def root_of(self, batch_id: int) -> bytes:
        self._check()
        batch = self.state.batches.get(batch_id)
        return batch.root if batch else ZERO_HASH

    def get_batch(self, batch_id: int) -> OnChainBatch | None:
        self._check()
        return self.state.batches.get(batch_id)

    def block_number(self) -> int:
        self._check()
        return self.state.block

    def block_hash(self, number: int) -> bytes | None:
        self._check()
        return self.state.block_hashes.get(number)

    def nonce(self, *, pending: bool) -> int:
        self._check()
        if pending and self.state.mempool:
            return max(self.state.nonce, max(self.state.mempool) + 1)
        return self.state.nonce

    def fees(self) -> tuple[int, int]:
        self._check()
        return self.base_fee * 2 + self.priority, self.priority

    def sign_anchor(
        self, call: AnchorCall, *, nonce: int, max_fee: int, priority_fee: int
    ) -> SignedAnchorTx:
        tx_hash = keccak256(
            repr((call, nonce, max_fee, priority_fee, len(self.signed))).encode("utf-8")
        )
        raw = b"raw:" + tx_hash
        self.signed[raw] = _Tx(tx_hash, call, nonce, max_fee, priority_fee)
        return SignedAnchorTx(tx_hash=tx_hash, raw=raw)

    def send_raw(self, raw: bytes) -> None:
        self._check()
        tx = self.signed[raw]
        self.broadcasts.append(tx.tx_hash)
        if tx.nonce < self.state.nonce:
            return  # "nonce too low": already mined or replaced
        current = self.state.mempool.get(tx.nonce)
        if current is None or current.tx_hash == tx.tx_hash or tx.max_fee > current.max_fee:
            self.state.mempool[tx.nonce] = tx

    def receipt(self, tx_hash: bytes) -> TxReceipt | None:
        self._check()
        return self.state.receipts.get(tx_hash)
