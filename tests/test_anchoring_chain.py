"""M21 A5 on a real chain: the batcher against AnchorRegistry deployed on Anvil (marker ``chain``).

Skipped unless `anvil` and `forge` are installed (Foundry). Each test gets a fresh Anvil with
automine off, so blocks are mined only when a test says so. Reorgs use evm_snapshot/evm_revert.
"""

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.models.anchor_batch import AnchorBatchStatus
from app.services.anchoring.batcher import AnchoringConfig, run_once
from app.services.anchoring.chain import Web3RegistryClient

from tests.test_anchoring_batcher import _batch, _db, _record_events

pytestmark = [
    pytest.mark.chain,
    pytest.mark.skipif(
        shutil.which("anvil") is None or shutil.which("forge") is None,
        reason="Foundry (anvil, forge) is not installed",
    ),
]

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
ARTIFACT = CONTRACTS / "out" / "AnchorRegistry.sol" / "AnchorRegistry.json"
# Anvil's first development account (public, test-only).
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CONFIG = AnchoringConfig(
    min_confirmations=2,
    fee_bump_after=timedelta(minutes=2),
    max_fee_per_gas=100 * 10**9,
    retry_backoff=timedelta(minutes=1),
)
T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def bytecode() -> str:
    if not ARTIFACT.exists():
        subprocess.run(["forge", "build"], cwd=CONTRACTS, check=True, capture_output=True)
    return json.loads(ARTIFACT.read_text())["bytecode"]["object"]


@pytest.fixture
def anvil(bytecode: str) -> Iterator[tuple[Web3RegistryClient, Any]]:
    from web3 import Web3

    port = _free_port()
    process = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent", "--no-mining"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    web3 = Web3(Web3.HTTPProvider(url))
    try:
        for _ in range(100):
            try:
                if web3.is_connected():
                    break
            except Exception:
                pass
            time.sleep(0.05)
        account = web3.eth.account.from_key(ANVIL_KEY)
        contract = web3.eth.contract(abi=json.loads(ARTIFACT.read_text())["abi"], bytecode=bytecode)
        deploy = contract.constructor(account.address).build_transaction(
            {"from": account.address, "nonce": 0, "chainId": 31337}
        )
        tx_hash = web3.eth.send_raw_transaction(account.sign_transaction(deploy).raw_transaction)
        web3.provider.make_request("evm_mine", [])
        address = web3.eth.get_transaction_receipt(tx_hash)["contractAddress"]
        client = Web3RegistryClient(
            rpc_url=url, chain_id=31337, registry_address=address, private_key=ANVIL_KEY
        )
        client.check_chain()
        yield client, web3
    finally:
        process.terminate()
        process.wait(timeout=5)


def _mine(web3: Any, blocks: int = 1) -> None:
    for _ in range(blocks):
        web3.provider.make_request("evm_mine", [])


def test_a_batch_is_anchored_and_confirmed_on_the_real_contract(anvil) -> None:
    client, web3 = anvil
    db = _db()
    _record_events(db, "anv1")

    assert run_once(db, client, CONFIG, now=T0).action == "submitted"
    _mine(web3)
    assert run_once(db, client, CONFIG, now=T0).action == "waiting"
    _mine(web3)
    assert run_once(db, client, CONFIG, now=T0).action == "confirmed"

    batch = _batch(db, 1)
    on_chain = client.get_batch(1)
    assert on_chain is not None
    assert (on_chain.root, on_chain.first_seq, on_chain.last_seq, on_chain.leaf_count) == (
        batch.root,
        batch.first_seq,
        batch.last_seq,
        batch.leaf_count,
    )
    assert client.batch_count() == 1

    _record_events(db, "anv2")
    assert run_once(db, client, CONFIG, now=T0).action == "submitted"
    _mine(web3, 2)
    assert run_once(db, client, CONFIG, now=T0).action == "confirmed"
    second = client.get_batch(2)
    assert second is not None and second.prev_root == batch.root


def test_a_reorg_drops_the_anchor_and_the_batcher_anchors_it_again(anvil) -> None:
    client, web3 = anvil
    db = _db()
    _record_events(db, "anv3")
    snapshot = web3.provider.make_request("evm_snapshot", [])["result"]

    assert run_once(db, client, CONFIG, now=T0).action == "submitted"
    _mine(web3, 2)
    assert run_once(db, client, CONFIG, now=T0).action == "confirmed"
    root = _batch(db, 1).root

    web3.provider.make_request("evm_revert", [snapshot])
    assert client.batch_count() == 0
    assert run_once(db, client, CONFIG, now=T0 + timedelta(minutes=1)).action == "submitted"
    _mine(web3, 2)
    assert run_once(db, client, CONFIG, now=T0 + timedelta(minutes=1)).action == "confirmed"
    assert client.root_of(1) == root
    assert _batch(db, 1).status == AnchorBatchStatus.CONFIRMED


def test_a_crash_after_the_write_ahead_record_is_repaired_by_a_rebroadcast(anvil) -> None:
    client, web3 = anvil
    db = _db()
    _record_events(db, "anv4")
    send = client.send_raw

    def crash(_raw: bytes) -> None:
        raise KeyboardInterrupt("killed between the record and the broadcast")

    client.send_raw = crash  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        run_once(db, client, CONFIG, now=T0)
    _mine(web3, 2)
    assert client.batch_count() == 0  # never reached the chain

    client.send_raw = send  # type: ignore[method-assign]
    assert run_once(db, client, CONFIG, now=T0).action == "waiting"
    _mine(web3, 2)
    assert run_once(db, client, CONFIG, now=T0).action == "confirmed"
    assert len(_batch(db, 1).tx_hashes.split(",")) == 1


def test_a_stuck_transaction_is_replaced_and_only_one_anchor_lands(anvil) -> None:
    client, web3 = anvil
    db = _db()
    _record_events(db, "anv5")
    assert run_once(db, client, CONFIG, now=T0).action == "submitted"
    assert run_once(db, client, CONFIG, now=T0 + timedelta(minutes=3)).action == "fee_bumped"
    _mine(web3, 2)
    assert run_once(db, client, CONFIG, now=T0 + timedelta(minutes=3)).action == "confirmed"
    assert client.batch_count() == 1
    assert len(_batch(db, 1).tx_hashes.split(",")) == 2


def test_an_unreachable_rpc_reports_and_leaves_the_batch_for_later() -> None:
    db = _db()
    _record_events(db, "anv6")
    client = Web3RegistryClient(
        rpc_url=f"http://127.0.0.1:{_free_port()}",
        chain_id=31337,
        registry_address="0x" + "11" * 20,
        private_key=ANVIL_KEY,
        timeout_seconds=0.5,
    )
    result = run_once(db, client, CONFIG, now=T0)
    assert result.action == "chain_unavailable"
    assert _batch(db, 1).status == AnchorBatchStatus.BUILT
