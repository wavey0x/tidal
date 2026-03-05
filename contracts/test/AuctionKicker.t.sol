// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {StdStorage, stdStorage} from "forge-std/StdStorage.sol";

import {AuctionKicker} from "../src/AuctionKicker.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {ITradeHandler} from "../src/interfaces/ITradeHandler.sol";
import {IAuction} from "../src/interfaces/IAuction.sol";

contract AuctionKickerTest is Test {
    using stdStorage for StdStorage;

    address internal constant TRADE_HANDLER = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;
    address internal constant AUCTION = 0x9D252f3da6E1c59EF1804657b59fC4129f70eD04;
    address internal constant ALT_AUCTION = 0x2232Fd50CBF9d500B4b624Bfe126F09caf3d24B8;
    address internal constant STRATEGY = 0x9AD3047D578e79187f0FaEEf26729097a4973325;
    address internal constant CRV = 0xD533a949740bb3306d119CC777fa900bA034cd52;

    address internal keeper = makeAddr("keeper");

    AuctionKicker internal kicker;

    function setUp() public {
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"));

        kicker = new AuctionKicker(TRADE_HANDLER);
        kicker.setKeeper(keeper, true);

        address governance = ITradeHandler(TRADE_HANDLER).governance();
        vm.prank(governance);
        ITradeHandler(TRADE_HANDLER).addMech(address(kicker));
    }

    function test_happyPath_transfers_setsPrice_kicks() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        vm.prank(keeper);
        kicker.kick(STRATEGY, AUCTION, CRV, amount, startingPrice);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(AUCTION).startingPrice(), startingPrice);
    }

    function test_revert_notKeeperOrOwner() public {
        vm.expectRevert(AuctionKicker.Unauthorized.selector);
        vm.prank(makeAddr("not-authorized"));
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, 1e18);
    }

    function test_revert_startingPriceZero() public {
        vm.expectRevert(AuctionKicker.StartingPriceZero.selector);
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, 0);
    }

    function test_revert_wantMismatch() public {
        vm.expectRevert(AuctionKicker.WantMismatch.selector);
        kicker.kick(STRATEGY, ALT_AUCTION, CRV, 1e18, 1e18);
    }
}
