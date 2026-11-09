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

**Deployed** (#279, `docs/anchoring.md` section 8): `0xD8094faf2B44711dD372A59C288130FA8113E4DB`
(`deployments/base-sepolia.json`, active), verified on Blockscout and Sourcify. The pilot
`0x8B3Fd6B4b31a24a6922A14Beb892BD695054de68` is retired (`deployments/base-sepolia-pilot-retired.json`).
A redeploy writes `deployments/base-sepolia.json`: rename the previous file to a `-retired` name and
mark it `"status": "retired"` first. `scripts/demo_tamper.py` reads every non-local deployment here
and refuses to run while the settings name one of them.

Verify the source after a deploy (no API key needed):

```bash
ARGS=$(cast abi-encode "constructor(address)" <anchorer>)
forge verify-contract <registry> src/AnchorRegistry.sol:AnchorRegistry --chain 84532 \
  --constructor-args $ARGS --verifier sourcify
forge verify-contract <registry> src/AnchorRegistry.sol:AnchorRegistry --chain 84532 \
  --constructor-args $ARGS --verifier blockscout --verifier-url https://base-sepolia.blockscout.com/api/
```
