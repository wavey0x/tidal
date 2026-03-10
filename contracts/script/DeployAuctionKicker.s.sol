// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";

import {AuctionKicker} from "../src/AuctionKicker.sol";

contract DeployAuctionKicker is Script {
    function run() external returns (AuctionKicker kicker) {
        vm.startBroadcast();
        kicker = new AuctionKicker();
        vm.stopBroadcast();
    }
}
