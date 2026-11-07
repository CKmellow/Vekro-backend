// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title AnchorRegistry
/// @notice Public, append-only record of Vekro's escrow event batches (M21, docs/anchoring-design.md).
/// Each batch is the RFC 6962 Merkle root (keccak256) of a gapless range of event sequence numbers.
/// Batches chain to each other through `prevRoot`, and the sequence ranges must join without a gap,
/// so the database cannot later drop or reorder anchored events without the chain disagreeing.
/// @dev No owner, no proxy, no overwrite and no delete (decision D6). Rotating the anchorer means
/// deploying a new registry and recording the switch-over (docs/runbooks.md).
contract AnchorRegistry {
    struct Batch {
        bytes32 root;
        bytes32 prevRoot;
        uint64 firstSeq;
        uint64 lastSeq;
        uint32 leafCount;
        uint64 anchoredAt;
        uint64 blockNumber;
    }

    /// @notice The only address allowed to anchor, fixed at deployment.
    address public immutable anchorer;

    /// @notice Number of anchored batches. Batch ids start at 1, so this is also the latest id.
    uint256 public batchCount;
    /// @notice The last anchored sequence number (0 before the first batch).
    uint64 public lastSeqAnchored;
    /// @notice The root of the latest batch (zero before the first batch).
    bytes32 public lastRoot;

    mapping(uint256 batchId => Batch) private _batches;
    /// @notice Batch id for an anchored root, or 0 when the root was never anchored.
    mapping(bytes32 root => uint256 batchId) public batchIdOfRoot;

    event BatchAnchored(
        uint256 indexed batchId,
        bytes32 indexed root,
        bytes32 prevRoot,
        uint32 leafCount,
        uint64 firstSeq,
        uint64 lastSeq
    );

    error ZeroAnchorer();
    error NotAnchorer(address caller);
    error UnexpectedBatchId(uint256 expected, uint256 actual);
    error PrevRootMismatch(bytes32 expected, bytes32 actual);
    error ZeroRoot();
    error EmptyBatch();
    error SeqGap(uint64 expectedFirstSeq, uint64 actualFirstSeq);
    error LeafCountMismatch(uint64 rangeSize, uint32 leafCount);
    error RootAlreadyAnchored(uint256 batchId);
    error UnknownBatch(uint256 batchId);

    constructor(address anchorer_) {
        if (anchorer_ == address(0)) revert ZeroAnchorer();
        anchorer = anchorer_;
    }

    /// @notice Anchor the next batch. Every argument is checked against the chain so far; the call
    /// reverts instead of storing anything that would break continuity.
    function anchor(uint256 batchId, bytes32 root, bytes32 prevRoot, uint32 leafCount, uint64 firstSeq, uint64 lastSeq)
        external
    {
        if (msg.sender != anchorer) revert NotAnchorer(msg.sender);
        if (batchId != batchCount + 1) revert UnexpectedBatchId(batchCount + 1, batchId);
        if (prevRoot != lastRoot) revert PrevRootMismatch(lastRoot, prevRoot);
        if (root == bytes32(0)) revert ZeroRoot();
        if (leafCount == 0) revert EmptyBatch();
        if (firstSeq != lastSeqAnchored + 1) revert SeqGap(lastSeqAnchored + 1, firstSeq);
        if (lastSeq < firstSeq || lastSeq - firstSeq + 1 != leafCount) {
            revert LeafCountMismatch(lastSeq < firstSeq ? 0 : lastSeq - firstSeq + 1, leafCount);
        }
        uint256 existing = batchIdOfRoot[root];
        if (existing != 0) revert RootAlreadyAnchored(existing);

        _batches[batchId] = Batch({
            root: root,
            prevRoot: prevRoot,
            firstSeq: firstSeq,
            lastSeq: lastSeq,
            leafCount: leafCount,
            anchoredAt: uint64(block.timestamp),
            blockNumber: uint64(block.number)
        });
        batchIdOfRoot[root] = batchId;
        batchCount = batchId;
        lastSeqAnchored = lastSeq;
        lastRoot = root;

        emit BatchAnchored(batchId, root, prevRoot, leafCount, firstSeq, lastSeq);
    }

    /// @notice A stored batch. Reverts for an id that was never anchored.
    function getBatch(uint256 batchId) external view returns (Batch memory) {
        if (batchId == 0 || batchId > batchCount) revert UnknownBatch(batchId);
        return _batches[batchId];
    }

    /// @notice The anchored root of a batch, or zero when the batch does not exist. Cheap for
    /// verifiers that only compare roots.
    function rootOf(uint256 batchId) external view returns (bytes32) {
        return _batches[batchId].root;
    }
}
