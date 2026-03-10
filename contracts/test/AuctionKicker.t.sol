// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {StdStorage, stdStorage} from "forge-std/StdStorage.sol";

import {AuctionKicker} from "../src/AuctionKicker.sol";
import {WeiRollCommandLib} from "../src/utils/WeiRollCommandLib.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {ITradeHandler} from "../src/interfaces/ITradeHandler.sol";
import {IAuction} from "../src/interfaces/IAuction.sol";

contract WeiRollCommandLibHarness {
    function cmdCall(bytes4 sel, uint8 a0, uint8 a1, uint8 a2, address target) external pure returns (bytes32) {
        return WeiRollCommandLib.cmdCall(sel, a0, a1, a2, target);
    }
}

contract AuctionKickerTest is Test {
    using stdStorage for StdStorage;

    event OwnerUpdated(address indexed owner);
    event KeeperUpdated(address indexed account, bool allowed);
    event Kicked(
        address indexed strategy, address indexed auction, address sellToken, uint256 sellAmount, uint256 startingPrice
    );

    address internal constant TRADE_HANDLER = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;
    address internal constant AUCTION = 0x9D252f3da6E1c59EF1804657b59fC4129f70eD04;
    address internal constant ALT_AUCTION = 0x2232Fd50CBF9d500B4b624Bfe126F09caf3d24B8;
    address internal constant STRATEGY = 0x9AD3047D578e79187f0FaEEf26729097a4973325;
    address internal constant CRV = 0xD533a949740bb3306d119CC777fa900bA034cd52;

    address internal keeper = makeAddr("keeper");
    address internal newOwner = makeAddr("new-owner");

    AuctionKicker internal kicker;
    WeiRollCommandLibHarness internal commandHarness;

    function setUp() public {
        vm.createSelectFork(vm.envString("MAINNET_URL"));

        kicker = new AuctionKicker();
        commandHarness = new WeiRollCommandLibHarness();
        kicker.setKeeper(keeper, true);

        address governance = ITradeHandler(TRADE_HANDLER).governance();
        vm.prank(governance);
        ITradeHandler(TRADE_HANDLER).addMech(address(kicker));

        stdstore.target(CRV).sig("allowance(address,address)").with_key(STRATEGY).with_key(TRADE_HANDLER).checked_write(
            type(uint256).max
        );
    }

    function test_constructor_emitsOwnerUpdated() public {
        vm.expectEmit(true, false, false, false);
        emit OwnerUpdated(address(this));

        new AuctionKicker();
    }

    function test_cmdCall_packsExpectedShortCommand() public view {
        bytes32 packed =
            commandHarness.cmdCall(bytes4(keccak256("transferFrom(address,address,uint256)")), 0, 1, 2, CRV);
        bytes32 expected = 0x23b872dd01000102ffffffffd533a949740bb3306d119cc777fa900ba034cd52;

        assertEq(packed, expected);
    }

    function test_setOwner_emitsOwnerUpdated() public {
        vm.expectEmit(true, false, false, false);
        emit OwnerUpdated(newOwner);

        kicker.setOwner(newOwner);

        assertEq(kicker.owner(), newOwner);
    }

    function test_setKeeper_emitsKeeperUpdated() public {
        address newKeeper = makeAddr("new-keeper");

        vm.expectEmit(true, false, false, true);
        emit KeeperUpdated(newKeeper, true);

        kicker.setKeeper(newKeeper, true);

        assertTrue(kicker.keeper(newKeeper));
    }

    function test_happyPath_transfers_setsPrice_kicks() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, AUCTION, CRV, amount, startingPrice);

        vm.prank(keeper);
        kicker.kick(STRATEGY, AUCTION, CRV, amount, startingPrice);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(AUCTION).startingPrice(), startingPrice);
    }

    function test_revert_notKeeperOrOwner() public {
        vm.expectRevert("unauthorized");
        vm.prank(makeAddr("not-authorized"));
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, 1e18);
    }

    function test_revert_startingPriceZero() public {
        vm.expectRevert("starting price zero");
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, 0);
    }

    function test_revert_wantMismatch() public {
        vm.expectRevert("want mismatch");
        kicker.kick(STRATEGY, ALT_AUCTION, CRV, 1e18, 1e18);
    }
}
