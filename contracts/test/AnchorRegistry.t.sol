// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {AnchorRegistry} from "../src/AnchorRegistry.sol";

contract AnchorRegistryTest is Test {
    AnchorRegistry internal registry;
    address internal anchorer = makeAddr("anchorer");

    event BatchAnchored(
        uint256 indexed batchId,
        bytes32 indexed root,
        bytes32 prevRoot,
        uint32 leafCount,
        uint64 firstSeq,
        uint64 lastSeq
    );

    function setUp() public {
        registry = new AnchorRegistry(anchorer);
    }

    function _anchor(uint256 batchId, bytes32 root, bytes32 prevRoot, uint32 count, uint64 first, uint64 last)
        internal
    {
        vm.prank(anchorer);
        registry.anchor(batchId, root, prevRoot, count, first, last);
    }

    function test_constructor_rejects_zero_anchorer() public {
        vm.expectRevert(AnchorRegistry.ZeroAnchorer.selector);
        new AnchorRegistry(address(0));
    }

    function test_first_batch_is_stored_and_emitted() public {
        vm.warp(1_700_000_000);
        vm.roll(42);
        vm.expectEmit(true, true, false, true, address(registry));
        emit BatchAnchored(1, keccak256("r1"), bytes32(0), 3, 1, 3);
        _anchor(1, keccak256("r1"), bytes32(0), 3, 1, 3);

        AnchorRegistry.Batch memory batch = registry.getBatch(1);
        assertEq(batch.root, keccak256("r1"));
        assertEq(batch.prevRoot, bytes32(0));
        assertEq(batch.firstSeq, 1);
        assertEq(batch.lastSeq, 3);
        assertEq(batch.leafCount, 3);
        assertEq(batch.anchoredAt, 1_700_000_000);
        assertEq(batch.blockNumber, 42);
        assertEq(registry.batchCount(), 1);
        assertEq(registry.lastSeqAnchored(), 3);
        assertEq(registry.lastRoot(), keccak256("r1"));
        assertEq(registry.batchIdOfRoot(keccak256("r1")), 1);
        assertEq(registry.rootOf(1), keccak256("r1"));
    }

    function test_batches_chain_by_prev_root_and_seq() public {
        _anchor(1, keccak256("r1"), bytes32(0), 3, 1, 3);
        _anchor(2, keccak256("r2"), keccak256("r1"), 1, 4, 4);
        assertEq(registry.getBatch(2).prevRoot, keccak256("r1"));
        assertEq(registry.lastSeqAnchored(), 4);
    }

    function test_reverts_for_anyone_but_the_anchorer() public {
        address stranger = makeAddr("stranger");
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.NotAnchorer.selector, stranger));
        registry.anchor(1, keccak256("r1"), bytes32(0), 1, 1, 1);
    }

    function test_reverts_on_a_skipped_or_repeated_batch_id() public {
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.UnexpectedBatchId.selector, 1, 2));
        registry.anchor(2, keccak256("r1"), bytes32(0), 1, 1, 1);

        _anchor(1, keccak256("r1"), bytes32(0), 1, 1, 1);
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.UnexpectedBatchId.selector, 2, 1));
        registry.anchor(1, keccak256("r2"), keccak256("r1"), 1, 2, 2);
    }

    function test_reverts_on_a_wrong_prev_root() public {
        _anchor(1, keccak256("r1"), bytes32(0), 1, 1, 1);
        vm.prank(anchorer);
        vm.expectRevert(
            abi.encodeWithSelector(AnchorRegistry.PrevRootMismatch.selector, keccak256("r1"), keccak256("x"))
        );
        registry.anchor(2, keccak256("r2"), keccak256("x"), 1, 2, 2);
    }

    function test_reverts_on_a_zero_root() public {
        vm.prank(anchorer);
        vm.expectRevert(AnchorRegistry.ZeroRoot.selector);
        registry.anchor(1, bytes32(0), bytes32(0), 1, 1, 1);
    }

    function test_reverts_on_an_empty_batch() public {
        vm.prank(anchorer);
        vm.expectRevert(AnchorRegistry.EmptyBatch.selector);
        registry.anchor(1, keccak256("r1"), bytes32(0), 0, 1, 0);
    }

    function test_reverts_on_a_seq_gap_or_overlap() public {
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.SeqGap.selector, 1, 2));
        registry.anchor(1, keccak256("r1"), bytes32(0), 1, 2, 2);

        _anchor(1, keccak256("r1"), bytes32(0), 3, 1, 3);
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.SeqGap.selector, 4, 3));
        registry.anchor(2, keccak256("r2"), keccak256("r1"), 1, 3, 3);
    }

    function test_reverts_when_the_leaf_count_does_not_match_the_range() public {
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.LeafCountMismatch.selector, 3, 2));
        registry.anchor(1, keccak256("r1"), bytes32(0), 2, 1, 3);

        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.LeafCountMismatch.selector, 0, 1));
        registry.anchor(1, keccak256("r1"), bytes32(0), 1, 1, 0);
    }

    function test_reverts_when_a_root_is_anchored_twice() public {
        _anchor(1, keccak256("r1"), bytes32(0), 1, 1, 1);
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.RootAlreadyAnchored.selector, 1));
        registry.anchor(2, keccak256("r1"), keccak256("r1"), 1, 2, 2);
    }

    function test_get_batch_reverts_for_unknown_ids() public {
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.UnknownBatch.selector, 0));
        registry.getBatch(0);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.UnknownBatch.selector, 1));
        registry.getBatch(1);
        assertEq(registry.rootOf(1), bytes32(0));
    }

    function testFuzz_valid_sequences_always_anchor(uint8 batches, uint32 seed) public {
        batches = uint8(bound(batches, 1, 40));
        bytes32 prev;
        uint64 next = 1;
        for (uint256 i = 1; i <= batches; i++) {
            uint32 count = uint32(bound(uint256(keccak256(abi.encode(seed, i))), 1, 500));
            bytes32 root = keccak256(abi.encode("root", seed, i));
            _anchor(i, root, prev, count, next, next + count - 1);
            prev = root;
            next += count;
        }
        assertEq(registry.batchCount(), batches);
        assertEq(registry.lastSeqAnchored(), next - 1);
        assertEq(registry.lastRoot(), prev);
    }

    function testFuzz_any_wrong_first_seq_reverts(uint64 firstSeq) public {
        _anchor(1, keccak256("r1"), bytes32(0), 5, 1, 5);
        vm.assume(firstSeq != 6 && firstSeq < type(uint64).max);
        vm.prank(anchorer);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.SeqGap.selector, 6, firstSeq));
        registry.anchor(2, keccak256("r2"), keccak256("r1"), 1, firstSeq, firstSeq);
    }

    function testFuzz_strangers_can_never_anchor(address caller) public {
        vm.assume(caller != anchorer);
        vm.prank(caller);
        vm.expectRevert(abi.encodeWithSelector(AnchorRegistry.NotAnchorer.selector, caller));
        registry.anchor(1, keccak256("r1"), bytes32(0), 1, 1, 1);
    }
}

/// Drives the registry with random valid and invalid calls for the invariant run.
contract RegistryHandler is Test {
    AnchorRegistry internal registry;
    address internal anchorer;
    bytes32[] public roots;
    uint64 public expectedLastSeq;

    constructor(AnchorRegistry registry_, address anchorer_) {
        registry = registry_;
        anchorer = anchorer_;
    }

    function anchorNext(uint32 count, uint256 salt) external {
        count = uint32(bound(count, 1, 1000));
        uint256 id = registry.batchCount() + 1;
        bytes32 root = keccak256(abi.encode(salt, id));
        if (registry.batchIdOfRoot(root) != 0) return;
        uint64 first = registry.lastSeqAnchored() + 1;
        // Read before the prank: the prank applies to the very next external call, views included.
        bytes32 prev = registry.lastRoot();
        vm.prank(anchorer);
        registry.anchor(id, root, prev, count, first, first + count - 1);
        roots.push(root);
        expectedLastSeq = first + count - 1;
    }

    function anchorBroken(uint256 batchId, bytes32 root, bytes32 prevRoot, uint32 count, uint64 first, uint64 last)
        external
    {
        vm.prank(anchorer);
        try registry.anchor(batchId, root, prevRoot, count, first, last) {
            // Only a call that happens to be valid can succeed; record it like a valid one.
            roots.push(root);
            expectedLastSeq = last;
        } catch {}
    }

    function rootCount() external view returns (uint256) {
        return roots.length;
    }
}

contract AnchorRegistryInvariantTest is StdInvariant, Test {
    AnchorRegistry internal registry;
    RegistryHandler internal handler;

    function setUp() public {
        address anchorer = makeAddr("anchorer");
        registry = new AnchorRegistry(anchorer);
        handler = new RegistryHandler(registry, anchorer);
        targetContract(address(handler));
    }

    /// Stored batches always form one unbroken chain: gapless seq ranges and linked roots.
    function invariant_batches_form_one_continuous_chain() public view {
        uint256 count = registry.batchCount();
        assertEq(count, handler.rootCount());
        bytes32 prev;
        uint64 next = 1;
        for (uint256 id = 1; id <= count; id++) {
            AnchorRegistry.Batch memory batch = registry.getBatch(id);
            assertEq(batch.prevRoot, prev);
            assertEq(batch.firstSeq, next);
            assertEq(uint256(batch.lastSeq - batch.firstSeq + 1), uint256(batch.leafCount));
            assertEq(batch.root, handler.roots(id - 1));
            assertEq(registry.batchIdOfRoot(batch.root), id);
            prev = batch.root;
            next = batch.lastSeq + 1;
        }
        assertEq(registry.lastRoot(), prev);
        assertEq(registry.lastSeqAnchored(), next - 1);
        assertEq(registry.lastSeqAnchored(), handler.expectedLastSeq());
    }
}
