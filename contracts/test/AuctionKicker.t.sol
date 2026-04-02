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

contract EnableAuctionMock {
    address public immutable governance;
    address[] internal enabled;

    constructor(address governance_) {
        governance = governance_;
    }

    function enable(address token) external {
        enabled.push(token);
    }

    function getAllEnabledAuctions() external view returns (address[] memory) {
        return enabled;
    }
}

contract AuctionKickerTest is Test {
    using stdStorage for StdStorage;

    event OwnerUpdated(address indexed owner);
    event KeeperUpdated(address indexed account, bool allowed);
    event Kicked(
        address indexed source,
        address indexed auction,
        address sellToken,
        uint256 sellAmount,
        uint256 startingPrice,
        uint256 minimumPrice,
        uint256 stepDecayRateBps,
        address settleToken
    );
    event SweepAndSettled(address indexed auction, address indexed sellToken);

    address internal constant TRADE_HANDLER = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;
    address internal constant AUCTION = 0x785cf728913e92DC5b24162DCBeE7A41E7de5747;
    address internal constant ALT_AUCTION = 0x1721A935063EcFBc1542f15E028e7c2FCe52B169;
    address internal constant STRATEGY = 0x9AD3047D578e79187f0FaEEf26729097a4973325;
    address internal constant FEE_BURNER = 0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8;
    address internal constant FEE_BURNER_AUCTION = 0xA00E6b35C23442fa9D5149Cba5dd94623fFE6693;
    address internal constant FEE_BURNER_WANT = 0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E;
    address internal constant CRV = 0xD533a949740bb3306d119CC777fa900bA034cd52;
    address internal constant CVX = 0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B;

    address internal keeper = makeAddr("keeper");
    address internal newOwner = makeAddr("new-owner");

    AuctionKicker internal kicker;
    WeiRollCommandLibHarness internal commandHarness;

    function setUp() public {
        vm.createSelectFork(vm.envString("MAINNET_URL"));

        address[] memory initialKeepers = new address[](1);
        initialKeepers[0] = keeper;

        kicker = new AuctionKicker(initialKeepers);
        commandHarness = new WeiRollCommandLibHarness();

        address governance = ITradeHandler(TRADE_HANDLER).governance();
        vm.prank(governance);
        ITradeHandler(TRADE_HANDLER).addMech(address(kicker));

        // CRV allowance + enable.
        stdstore.target(CRV).sig("allowance(address,address)").with_key(STRATEGY).with_key(TRADE_HANDLER).checked_write(
            type(uint256).max
        );
        vm.prank(TRADE_HANDLER);
        _enableIfNeeded(AUCTION, CRV);

        // CVX allowance + enable (for batch tests).
        stdstore.target(CVX).sig("allowance(address,address)").with_key(STRATEGY).with_key(TRADE_HANDLER).checked_write(
            type(uint256).max
        );
        vm.prank(TRADE_HANDLER);
        _enableIfNeeded(AUCTION, CVX);
    }

    function _enableIfNeeded(address auction, address token) internal {
        try IAuction(auction).enable(token) {}
        catch Error(string memory reason) {
            if (keccak256(bytes(reason)) != keccak256(bytes("already enabled"))) {
                revert(reason);
            }
        }
    }

    // -----------------------------------------------------------------------
    // Existing tests (kick convenience wrapper)
    // -----------------------------------------------------------------------

    function test_constructor_emitsOwnerUpdated() public {
        address[] memory initialKeepers = new address[](0);

        vm.expectEmit(true, false, false, false);
        emit OwnerUpdated(address(this));

        new AuctionKicker(initialKeepers);
    }

    function test_constructor_setsInitialKeepers_andEmitsKeeperUpdated() public {
        address firstKeeper = makeAddr("first-keeper");
        address secondKeeper = makeAddr("second-keeper");
        address[] memory initialKeepers = new address[](2);
        initialKeepers[0] = firstKeeper;
        initialKeepers[1] = secondKeeper;

        vm.expectEmit(true, false, false, false);
        emit OwnerUpdated(address(this));
        vm.expectEmit(true, false, false, true);
        emit KeeperUpdated(firstKeeper, true);
        vm.expectEmit(true, false, false, true);
        emit KeeperUpdated(secondKeeper, true);

        AuctionKicker deployed = new AuctionKicker(initialKeepers);

        assertTrue(deployed.keeper(firstKeeper));
        assertTrue(deployed.keeper(secondKeeper));
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
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;
        address wantToken = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, AUCTION, CRV, amount, startingPrice, minimumPrice, stepDecayRateBps, address(0));

        vm.prank(keeper);
        kicker.kick(
            STRATEGY, AUCTION, CRV, amount, wantToken, startingPrice, minimumPrice, stepDecayRateBps, address(0)
        );

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(AUCTION).startingPrice(), startingPrice);
        assertEq(IAuction(AUCTION).minimumPrice(), minimumPrice);
        assertEq(IAuction(AUCTION).stepDecayRate(), stepDecayRateBps);
    }

    function test_kickExtended_happyPath_transfers_setsPrice_kicks() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;
        address wantToken = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        AuctionKicker.KickParamsExtended memory params = AuctionKicker.KickParamsExtended({
            source: STRATEGY,
            auction: AUCTION,
            sellToken: CRV,
            sellAmount: amount,
            wantToken: wantToken,
            startingPrice: startingPrice,
            minimumPrice: minimumPrice,
            stepDecayRateBps: stepDecayRateBps,
            settleToken: address(0),
            settleAfterStart: new address[](0),
            settleAfterMin: new address[](0),
            settleAfterDecay: new address[](0)
        });

        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, AUCTION, CRV, amount, startingPrice, minimumPrice, stepDecayRateBps, address(0));

        vm.prank(keeper);
        kicker.kickExtended(params);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(AUCTION).startingPrice(), startingPrice);
        assertEq(IAuction(AUCTION).minimumPrice(), minimumPrice);
        assertEq(IAuction(AUCTION).stepDecayRate(), stepDecayRateBps);
    }

    function test_revert_notKeeperOrOwner() public {
        address wantToken = IAuction(AUCTION).want();
        vm.expectRevert("unauthorized");
        vm.prank(makeAddr("not-authorized"));
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, wantToken, 1e18, 0, 50, address(0));
    }

    function test_revert_startingPriceZero() public {
        address wantToken = IAuction(AUCTION).want();
        vm.expectRevert("starting price zero");
        kicker.kick(STRATEGY, AUCTION, CRV, 1e18, wantToken, 0, 0, 50, address(0));
    }

    function test_revert_wantMismatch() public {
        address wantToken = IAuction(AUCTION).want();
        vm.expectRevert("want mismatch");
        kicker.kick(STRATEGY, ALT_AUCTION, CRV, 1e18, wantToken, 1e18, 0, 50, address(0));
    }

    function test_revert_sellTokenIsWant() public {
        address wantToken = IAuction(AUCTION).want();
        vm.expectRevert("sell token is want");
        kicker.kick(STRATEGY, AUCTION, wantToken, 1e18, wantToken, 1e18, 0, 50, address(0));
    }

    function test_revert_receiverMismatch() public {
        address strategyWant = IAuction(AUCTION).want();
        vm.mockCall(ALT_AUCTION, abi.encodeWithSelector(IAuction.want.selector), abi.encode(strategyWant));

        vm.expectRevert("receiver mismatch");
        kicker.kick(STRATEGY, ALT_AUCTION, CRV, 1e18, strategyWant, 1e18, 0, 50, address(0));
    }

    function test_minimumPrice_zeroAllowed() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;
        uint256 stepDecayRateBps = 50;
        address wantToken = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        vm.prank(keeper);
        kicker.kick(STRATEGY, AUCTION, CRV, amount, wantToken, startingPrice, 0, stepDecayRateBps, address(0));

        assertEq(IAuction(AUCTION).minimumPrice(), 0);
    }

    // -----------------------------------------------------------------------
    // batchKick tests
    // -----------------------------------------------------------------------

    function test_batchKick_singleItem() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;
        address wantToken = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.warp(block.timestamp + 8 days);

        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](1);
        kicks[0] = AuctionKicker.KickParams(
            STRATEGY, AUCTION, CRV, amount, wantToken, startingPrice, minimumPrice, stepDecayRateBps, address(0)
        );

        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, AUCTION, CRV, amount, startingPrice, minimumPrice, stepDecayRateBps, address(0));

        vm.prank(keeper);
        kicker.batchKick(kicks);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(AUCTION).startingPrice(), startingPrice);
    }

    function test_batchKick_multipleItems() public {
        // Two kicks targeting different auctions (same auction goes "active" after first kick).
        uint256 amount1 = 50e18;
        uint256 amount2 = 30e18;
        uint256 startingPrice = 2e18;
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;

        // Mock ALT_AUCTION to accept our strategy (want match + receiver match).
        address strategyWant = IAuction(AUCTION).want();
        vm.mockCall(ALT_AUCTION, abi.encodeWithSelector(IAuction.want.selector), abi.encode(strategyWant));
        vm.mockCall(ALT_AUCTION, abi.encodeWithSelector(IAuction.receiver.selector), abi.encode(STRATEGY));

        // Enable CRV on ALT_AUCTION.
        vm.prank(TRADE_HANDLER);
        _enableIfNeeded(ALT_AUCTION, CRV);

        // Fund strategy for both kicks.
        uint256 crvBaseBal = IERC20(CRV).balanceOf(STRATEGY);
        deal(CRV, STRATEGY, crvBaseBal + amount1 + amount2);

        uint256 auction1Before = IERC20(CRV).balanceOf(AUCTION);
        uint256 auction2Before = IERC20(CRV).balanceOf(ALT_AUCTION);

        vm.warp(block.timestamp + 8 days);

        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](2);
        kicks[0] = AuctionKicker.KickParams(
            STRATEGY, AUCTION, CRV, amount1, strategyWant, startingPrice, minimumPrice, stepDecayRateBps, address(0)
        );
        kicks[1] = AuctionKicker.KickParams(
            STRATEGY, ALT_AUCTION, CRV, amount2, strategyWant, startingPrice, minimumPrice, stepDecayRateBps, address(0)
        );

        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, AUCTION, CRV, amount1, startingPrice, minimumPrice, stepDecayRateBps, address(0));
        vm.expectEmit(true, true, false, true);
        emit Kicked(STRATEGY, ALT_AUCTION, CRV, amount2, startingPrice, minimumPrice, stepDecayRateBps, address(0));

        vm.prank(keeper);
        kicker.batchKick(kicks);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), crvBaseBal);
        assertEq(IERC20(CRV).balanceOf(AUCTION), auction1Before + amount1);
        assertEq(IERC20(CRV).balanceOf(ALT_AUCTION), auction2Before + amount2);
    }

    function test_batchKick_revert_oneItemFails() public {
        uint256 amount = 50e18;
        uint256 crvBaseBal = IERC20(CRV).balanceOf(STRATEGY);
        deal(CRV, STRATEGY, crvBaseBal + amount);

        vm.warp(block.timestamp + 8 days);

        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](2);
        kicks[0] = AuctionKicker.KickParams(
            STRATEGY, AUCTION, CRV, amount, IAuction(AUCTION).want(), 2e18, 1e18, 50, address(0)
        );
        kicks[1] =
            AuctionKicker.KickParams(STRATEGY, AUCTION, CVX, amount, IAuction(AUCTION).want(), 0, 0, 50, address(0)); // startingPrice = 0 → revert

        vm.expectRevert("starting price zero");
        vm.prank(keeper);
        kicker.batchKick(kicks);

        // First kick's transfer should be rolled back.
        assertEq(IERC20(CRV).balanceOf(STRATEGY), crvBaseBal + amount);
    }

    function test_batchKick_emptyArray() public {
        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](0);

        vm.prank(keeper);
        kicker.batchKick(kicks);
        // No revert, no events — just a no-op.
    }

    function test_batchKick_revert_unauthorized() public {
        address wantToken = IAuction(AUCTION).want();
        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](1);
        kicks[0] = AuctionKicker.KickParams(STRATEGY, AUCTION, CRV, 1e18, wantToken, 1e18, 0, 50, address(0));

        vm.expectRevert("unauthorized");
        vm.prank(makeAddr("not-authorized"));
        kicker.batchKick(kicks);
    }

    function test_feeBurner_happyPath_transfers_setsPrice_kicks() public {
        uint256 amount = 100e18;
        uint256 startingPrice = 2e18;
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;

        uint256 burnerBaseBalance = IERC20(CRV).balanceOf(FEE_BURNER);
        uint256 auctionStartBalance = IERC20(CRV).balanceOf(FEE_BURNER_AUCTION);
        deal(CRV, FEE_BURNER, burnerBaseBalance + amount);

        stdstore.target(CRV).sig("allowance(address,address)").with_key(FEE_BURNER).with_key(TRADE_HANDLER)
            .checked_write(type(uint256).max);
        vm.prank(TRADE_HANDLER);
        _enableIfNeeded(FEE_BURNER_AUCTION, CRV);

        vm.warp(block.timestamp + 8 days);

        vm.expectEmit(true, true, false, true);
        emit Kicked(
            FEE_BURNER, FEE_BURNER_AUCTION, CRV, amount, startingPrice, minimumPrice, stepDecayRateBps, address(0)
        );

        vm.prank(keeper);
        kicker.kick(
            FEE_BURNER,
            FEE_BURNER_AUCTION,
            CRV,
            amount,
            FEE_BURNER_WANT,
            startingPrice,
            minimumPrice,
            stepDecayRateBps,
            address(0)
        );

        assertEq(IERC20(CRV).balanceOf(FEE_BURNER), burnerBaseBalance);
        assertEq(IERC20(CRV).balanceOf(FEE_BURNER_AUCTION), auctionStartBalance + amount);
        assertEq(IAuction(FEE_BURNER_AUCTION).startingPrice(), startingPrice);
        assertEq(IAuction(FEE_BURNER_AUCTION).minimumPrice(), minimumPrice);
        assertEq(IAuction(FEE_BURNER_AUCTION).stepDecayRate(), stepDecayRateBps);
    }

    function test_batchKick_mixedStrategyAndFeeBurner() public {
        uint256 strategyAmount = 40e18;
        uint256 burnerAmount = 60e18;
        uint256 startingPrice = 2e18;
        uint256 minimumPrice = 1e18;
        uint256 stepDecayRateBps = 50;
        address strategyWant = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 burnerBaseBalance = IERC20(CRV).balanceOf(FEE_BURNER);
        uint256 strategyAuctionBefore = IERC20(CRV).balanceOf(AUCTION);
        uint256 burnerAuctionBefore = IERC20(CRV).balanceOf(FEE_BURNER_AUCTION);

        deal(CRV, STRATEGY, strategyBaseBalance + strategyAmount);
        deal(CRV, FEE_BURNER, burnerBaseBalance + burnerAmount);
        stdstore.target(CRV).sig("allowance(address,address)").with_key(FEE_BURNER).with_key(TRADE_HANDLER)
            .checked_write(type(uint256).max);
        vm.prank(TRADE_HANDLER);
        _enableIfNeeded(FEE_BURNER_AUCTION, CRV);

        vm.warp(block.timestamp + 8 days);

        AuctionKicker.KickParams[] memory kicks = new AuctionKicker.KickParams[](2);
        kicks[0] = AuctionKicker.KickParams(
            STRATEGY,
            AUCTION,
            CRV,
            strategyAmount,
            strategyWant,
            startingPrice,
            minimumPrice,
            stepDecayRateBps,
            address(0)
        );
        kicks[1] = AuctionKicker.KickParams(
            FEE_BURNER,
            FEE_BURNER_AUCTION,
            CRV,
            burnerAmount,
            FEE_BURNER_WANT,
            startingPrice,
            minimumPrice,
            stepDecayRateBps,
            address(0)
        );

        vm.prank(keeper);
        kicker.batchKick(kicks);

        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance);
        assertEq(IERC20(CRV).balanceOf(FEE_BURNER), burnerBaseBalance);
        assertEq(IERC20(CRV).balanceOf(AUCTION), strategyAuctionBefore + strategyAmount);
        assertEq(IERC20(CRV).balanceOf(FEE_BURNER_AUCTION), burnerAuctionBefore + burnerAmount);
    }

    function test_sweepAndSettle_clearsActiveAuction() public {
        uint256 amount = 15e18;
        address wantToken = IAuction(AUCTION).want();

        uint256 strategyBaseBalance = IERC20(CRV).balanceOf(STRATEGY);
        uint256 auctionBalanceBefore = IERC20(CRV).balanceOf(AUCTION);
        deal(CRV, STRATEGY, strategyBaseBalance + amount);

        vm.prank(keeper);
        kicker.kick(STRATEGY, AUCTION, CRV, amount, wantToken, 2e18, 1e18, 50, address(0));

        assertTrue(IAuction(AUCTION).isActive(CRV));
        assertEq(IERC20(CRV).balanceOf(AUCTION), auctionBalanceBefore + amount);

        vm.expectEmit(true, true, false, true);
        emit SweepAndSettled(AUCTION, CRV);

        vm.prank(keeper);
        kicker.sweepAndSettle(AUCTION, CRV);

        assertFalse(IAuction(AUCTION).isActive(CRV));
        assertEq(IERC20(CRV).balanceOf(AUCTION), 0);
        assertEq(IERC20(CRV).balanceOf(STRATEGY), strategyBaseBalance + amount);
    }

    function test_enableTokens_keeperEnablesTokenThroughTradeHandler() public {
        EnableAuctionMock mockAuction = new EnableAuctionMock(TRADE_HANDLER);
        address[] memory sellTokens = new address[](1);
        sellTokens[0] = CVX;

        vm.prank(keeper);
        kicker.enableTokens(address(mockAuction), sellTokens);

        address[] memory enabled = mockAuction.getAllEnabledAuctions();
        bool found;
        for (uint256 i = 0; i < enabled.length; i++) {
            if (enabled[i] == CVX) {
                found = true;
                break;
            }
        }
        assertTrue(found);
    }

    function test_enableTokens_revert_notKeeperOrOwner() public {
        address[] memory sellTokens = new address[](1);
        sellTokens[0] = CVX;

        vm.expectRevert("unauthorized");
        vm.prank(makeAddr("not-authorized"));
        kicker.enableTokens(ALT_AUCTION, sellTokens);
    }

    function test_enableTokens_revert_governanceMismatch() public {
        address fakeAuction = makeAddr("fake-auction");
        vm.mockCall(fakeAuction, abi.encodeWithSelector(IAuction.governance.selector), abi.encode(makeAddr("other")));

        address[] memory sellTokens = new address[](1);
        sellTokens[0] = CVX;

        vm.expectRevert("governance mismatch");
        vm.prank(keeper);
        kicker.enableTokens(fakeAuction, sellTokens);
    }
}
