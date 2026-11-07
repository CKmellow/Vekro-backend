"""The AnchorRegistry as the batcher and the verifiers see it (M21 A5).

``RegistryClient`` is the small surface the submitter needs: reads of the registry, the
anchorer's nonces, fee estimates, signing and broadcasting a raw transaction, and receipts.
``Web3RegistryClient`` implements it over JSON-RPC with web3 7.x. Tests use an in-memory
implementation that applies the contract's own rules (tests/anchoring_fakes.py) and, under the
``chain`` marker, a real Anvil.

Any RPC failure surfaces as ``ChainUnavailableError`` so callers can tell "the chain is down"
(retry later, escrow unaffected) apart from programming errors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.services.anchoring.chain_config import secret_value

ABI_PATH = Path(__file__).resolve().parents[3] / "contracts" / "abi" / "AnchorRegistry.json"
ANCHOR_GAS_LIMIT = 250_000


class ChainUnavailableError(RuntimeError):
    """The RPC endpoint could not be reached or answered with an error."""


@dataclass(frozen=True)
class OnChainBatch:
    batch_id: int
    root: bytes
    prev_root: bytes
    first_seq: int
    last_seq: int
    leaf_count: int
    anchored_at: int
    block_number: int


@dataclass(frozen=True)
class SignedAnchorTx:
    tx_hash: bytes
    raw: bytes


@dataclass(frozen=True)
class TxReceipt:
    tx_hash: bytes
    succeeded: bool
    block_number: int
    block_hash: bytes


@dataclass(frozen=True)
class AnchorCall:
    batch_id: int
    root: bytes
    prev_root: bytes
    leaf_count: int
    first_seq: int
    last_seq: int


class RegistryClient(Protocol):
    chain_id: int
    registry_address: str
    anchorer_address: str

    def batch_count(self) -> int: ...

    def root_of(self, batch_id: int) -> bytes: ...

    def get_batch(self, batch_id: int) -> OnChainBatch | None: ...

    def block_number(self) -> int: ...

    def block_hash(self, number: int) -> bytes | None: ...

    def nonce(self, *, pending: bool) -> int: ...

    def fees(self) -> tuple[int, int]:
        """(max_fee_per_gas, max_priority_fee_per_gas) in wei."""
        ...

    def sign_anchor(
        self, call: AnchorCall, *, nonce: int, max_fee: int, priority_fee: int
    ) -> SignedAnchorTx: ...

    def send_raw(self, raw: bytes) -> None:
        """Broadcast; an "already known" answer for the same transaction is not an error."""
        ...

    def receipt(self, tx_hash: bytes) -> TxReceipt | None: ...


@lru_cache
def registry_abi() -> list[dict[str, Any]]:
    return json.loads(ABI_PATH.read_text(encoding="utf-8"))


_ALREADY_KNOWN = ("already known", "known transaction", "nonce too low", "already imported")


class Web3RegistryClient:
    def __init__(
        self,
        *,
        rpc_url: str,
        chain_id: int,
        registry_address: str,
        private_key: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        from web3 import Web3

        self._web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout_seconds}))
        self.chain_id = chain_id
        self.registry_address = Web3.to_checksum_address(registry_address)
        self._contract = self._web3.eth.contract(address=self.registry_address, abi=registry_abi())
        self._account = None
        self.anchorer_address = ""
        if private_key:
            from eth_account import Account

            self._account = Account.from_key(private_key)
            self.anchorer_address = self._account.address

    def _call(self, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # web3 raises many transport and RPC error types
            raise ChainUnavailableError(type(exc).__name__ + ": " + str(exc)[:200]) from exc

    def check_chain(self) -> None:
        actual = self._call(lambda: self._web3.eth.chain_id)
        if actual != self.chain_id:
            raise ChainUnavailableError(f"RPC is on chain {actual}, expected {self.chain_id}")

    def batch_count(self) -> int:
        return int(self._call(lambda: self._contract.functions.batchCount().call()))

    def root_of(self, batch_id: int) -> bytes:
        return bytes(self._call(lambda: self._contract.functions.rootOf(batch_id).call()))

    def get_batch(self, batch_id: int) -> OnChainBatch | None:
        if batch_id < 1 or batch_id > self.batch_count():
            return None
        raw = self._call(lambda: self._contract.functions.getBatch(batch_id).call())
        root, prev_root, first_seq, last_seq, leaf_count, anchored_at, block_number = raw
        return OnChainBatch(
            batch_id=batch_id,
            root=bytes(root),
            prev_root=bytes(prev_root),
            first_seq=int(first_seq),
            last_seq=int(last_seq),
            leaf_count=int(leaf_count),
            anchored_at=int(anchored_at),
            block_number=int(block_number),
        )

    def block_number(self) -> int:
        return int(self._call(lambda: self._web3.eth.block_number))

    def block_hash(self, number: int) -> bytes | None:
        block = self._call(lambda: self._web3.eth.get_block(number))
        return bytes(block["hash"]) if block else None

    def nonce(self, *, pending: bool) -> int:
        tag = "pending" if pending else "latest"
        return int(
            self._call(lambda: self._web3.eth.get_transaction_count(self.anchorer_address, tag))
        )

    def fees(self) -> tuple[int, int]:
        def estimate() -> tuple[int, int]:
            base = int(self._web3.eth.get_block("latest")["baseFeePerGas"])
            priority = int(self._web3.eth.max_priority_fee)
            return base * 2 + priority, priority

        return self._call(estimate)

    def sign_anchor(
        self, call: AnchorCall, *, nonce: int, max_fee: int, priority_fee: int
    ) -> SignedAnchorTx:
        if self._account is None:
            raise RuntimeError("No anchorer key configured.")
        tx = self._contract.functions.anchor(
            call.batch_id,
            call.root,
            call.prev_root,
            call.leaf_count,
            call.first_seq,
            call.last_seq,
        ).build_transaction(
            {
                "chainId": self.chain_id,
                "from": self.anchorer_address,
                "nonce": nonce,
                "gas": ANCHOR_GAS_LIMIT,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
            }
        )
        signed = self._account.sign_transaction(tx)
        return SignedAnchorTx(tx_hash=bytes(signed.hash), raw=bytes(signed.raw_transaction))

    def send_raw(self, raw: bytes) -> None:
        try:
            self._web3.eth.send_raw_transaction(raw)
        except Exception as exc:
            if any(marker in str(exc).lower() for marker in _ALREADY_KNOWN):
                return
            raise ChainUnavailableError(type(exc).__name__ + ": " + str(exc)[:200]) from exc

    def receipt(self, tx_hash: bytes) -> TxReceipt | None:
        from web3.exceptions import TransactionNotFound

        try:
            raw = self._web3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        except Exception as exc:
            raise ChainUnavailableError(type(exc).__name__ + ": " + str(exc)[:200]) from exc
        return TxReceipt(
            tx_hash=tx_hash,
            succeeded=int(raw["status"]) == 1,
            block_number=int(raw["blockNumber"]),
            block_hash=bytes(raw["blockHash"]),
        )


def build_registry_client(settings: Any, *, with_key: bool = True) -> Web3RegistryClient:
    return Web3RegistryClient(
        rpc_url=settings.anchor_rpc_url,
        chain_id=settings.anchor_chain_id,
        registry_address=settings.anchor_registry_address,
        private_key=secret_value(settings.anchor_private_key) if with_key else "",
        timeout_seconds=settings.anchor_rpc_timeout_seconds,
    )
