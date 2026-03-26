// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";

import {AuctionKicker} from "../src/AuctionKicker.sol";

contract DeployAuctionKicker is Script {
    address internal constant DEFAULT_KEEPER = 0xA009Cf8B0eDddf58A3c32Be2D85859fA494b12e3;

    function run() external returns (AuctionKicker kicker) {
        address[] memory initialKeepers = new address[](1);
        initialKeepers[0] = DEFAULT_KEEPER;

        vm.startBroadcast();
        kicker = new AuctionKicker(initialKeepers);
        vm.stopBroadcast();
    }
}
