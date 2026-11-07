# AnchorRegistry (M21 A4)

The public, append-only registry that Vekro anchors its escrow event batches to
(`docs/anchoring-design.md`). Each batch stores the RFC 6962 keccak Merkle root of a gapless range of
event sequence numbers. Batches link through `prevRoot`. The contract refuses anything that would
break that chain:

| Check | Revert |
| --- | --- |
| caller is the immutable `anchorer` | `NotAnchorer` |
| `batchId == batchCount + 1` | `UnexpectedBatchId` |
| `prevRoot == lastRoot` | `PrevRootMismatch` |
| `root != 0` | `ZeroRoot` |
| `leafCount > 0` | `EmptyBatch` |
| `firstSeq == lastSeqAnchored + 1` | `SeqGap` |
| `lastSeq - firstSeq + 1 == leafCount` | `LeafCountMismatch` |
| a root is anchored once | `RootAlreadyAnchored` |

There is no owner, no proxy, no overwrite and no delete (decision D6). To rotate the anchorer key,
deploy a new registry and record the switch-over (`docs/runbooks.md`).

Views: `getBatch(id)` (reverts `UnknownBatch`), `rootOf(id)` (zero when unknown), `batchIdOfRoot(root)`,
`batchCount`, `lastSeqAnchored`, `lastRoot`, `anchorer`. Each anchor emits `BatchAnchored`.

## Commands

```bash
git submodule update --init --recursive   # forge-std
make contracts-test                       # fmt check + unit, fuzz and invariant tests
make contracts-abi                        # refresh abi/AnchorRegistry.json after a contract change
anvil --port 8545 &                       # local chain 31337
make anvil-deploy                         # writes deployments/anvil.json
```

`abi/AnchorRegistry.json` is committed for the backend, and CI fails when it drifts from the build.

## Base Sepolia (84532)

Do not deploy without the maintainer's explicit go-ahead and a funded testnet key from them. The
script refuses to run unless `CONFIRM_PUBLIC_DEPLOY` names the network:

```bash
CONFIRM_PUBLIC_DEPLOY=base-sepolia BASE_SEPOLIA_RPC_URL=... ANCHORER_ADDRESS=0x... \
  forge script script/Deploy.s.sol --rpc-url base_sepolia --broadcast --private-key <funded key>
```

`deployments/<network>.json` records `chainId`, `registry`, `anchorer` and `fromBlock` (where to start
scanning `BatchAnchored` logs). Commit it after a deploy.
