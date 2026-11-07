// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {AnchorRegistry} from "../src/AnchorRegistry.sol";

/// Deploys AnchorRegistry and records it in deployments/<network>.json.
///
/// Anvil:        forge script script/Deploy.s.sol --rpc-url anvil --broadcast \
///                 --private-key <anvil key>
/// Base Sepolia: CONFIRM_PUBLIC_DEPLOY=base-sepolia forge script script/Deploy.s.sol \
///                 --rpc-url base_sepolia --broadcast --private-key <funded key>
///
/// ANCHORER_ADDRESS sets the anchorer; it defaults to the deploying address. A public network
/// deploy refuses to run unless CONFIRM_PUBLIC_DEPLOY names that network, so it can never happen
/// by accident (the maintainer supplies the funded key and says go).
contract Deploy is Script {
    uint256 internal constant ANVIL = 31337;
    uint256 internal constant BASE_SEPOLIA = 84532;

    error UnsupportedChain(uint256 chainId);
    error PublicDeployNotConfirmed(string network);

    function run() external returns (AnchorRegistry registry) {
        string memory network = networkName(block.chainid);
        if (block.chainid != ANVIL) {
            string memory confirmed = vm.envOr("CONFIRM_PUBLIC_DEPLOY", string(""));
            if (keccak256(bytes(confirmed)) != keccak256(bytes(network))) {
                revert PublicDeployNotConfirmed(network);
            }
        }

        vm.startBroadcast();
        (, address sender,) = vm.readCallers();
        address anchorer = vm.envOr("ANCHORER_ADDRESS", sender);
        registry = new AnchorRegistry(anchorer);
        vm.stopBroadcast();

        string memory key = "deployment";
        vm.serializeUint(key, "chainId", block.chainid);
        vm.serializeString(key, "network", network);
        vm.serializeAddress(key, "anchorer", anchorer);
        vm.serializeUint(key, "fromBlock", block.number);
        string memory json = vm.serializeAddress(key, "registry", address(registry));
        vm.writeJson(json, string.concat("deployments/", network, ".json"));
        console2.log("AnchorRegistry", address(registry), "on", network);
    }

    function networkName(uint256 chainId) public pure returns (string memory) {
        if (chainId == ANVIL) return "anvil";
        if (chainId == BASE_SEPOLIA) return "base-sepolia";
        revert UnsupportedChain(chainId);
    }
}
