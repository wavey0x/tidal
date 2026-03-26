// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {ITradeHandler} from "./interfaces/ITradeHandler.sol";
import {IAuction} from "./interfaces/IAuction.sol";
import {WeiRollCommandLib} from "./utils/WeiRollCommandLib.sol";

contract AuctionKicker {
    bytes4 internal constant TRANSFER_SELECTOR = bytes4(keccak256("transfer(address,uint256)"));
    bytes4 internal constant TRANSFER_FROM_SELECTOR = bytes4(keccak256("transferFrom(address,address,uint256)"));
    bytes4 internal constant SET_STARTING_PRICE_SELECTOR = bytes4(keccak256("setStartingPrice(uint256)"));
    bytes4 internal constant SET_MINIMUM_PRICE_SELECTOR = bytes4(keccak256("setMinimumPrice(uint256)"));
    bytes4 internal constant SET_STEP_DECAY_RATE_SELECTOR = bytes4(keccak256("setStepDecayRate(uint256)"));
    bytes4 internal constant SETTLE_SELECTOR = bytes4(keccak256("settle(address)"));
    bytes4 internal constant SWEEP_SELECTOR = bytes4(keccak256("sweep(address)"));
    bytes4 internal constant KICK_SELECTOR = bytes4(keccak256("kick(address)"));
    address public constant tradeHandler = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;

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

    struct KickParams {
        address source;
        address auction;
        address sellToken;
        uint256 sellAmount;
        address wantToken;
        uint256 startingPrice;
        uint256 minimumPrice;
        uint256 stepDecayRateBps;
        address settleToken;
    }

    address public owner;
    mapping(address => bool) public keeper;

    constructor(address[] memory initialKeepers) {
        owner = msg.sender;
        emit OwnerUpdated(msg.sender);

        for (uint256 i = 0; i < initialKeepers.length; i++) {
            _setKeeper(initialKeepers[i], true);
        }
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "unauthorized");
        _;
    }

    modifier onlyKeeperOrOwner() {
        require(msg.sender == owner || keeper[msg.sender], "unauthorized");
        _;
    }

    function setOwner(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero address");
        owner = newOwner;
        emit OwnerUpdated(newOwner);
    }

    function setKeeper(address account, bool allowed) external onlyOwner {
        _setKeeper(account, allowed);
    }

    function _setKeeper(address account, bool allowed) internal {
        keeper[account] = allowed;
        emit KeeperUpdated(account, allowed);
    }

    function kick(
        address source,
        address auction,
        address sellToken,
        uint256 sellAmount,
        address wantToken,
        uint256 startingPrice,
        uint256 minimumPrice,
        uint256 stepDecayRateBps,
        address settleToken
    )
        external
        onlyKeeperOrOwner
    {
        _kick(KickParams(source, auction, sellToken, sellAmount, wantToken, startingPrice, minimumPrice, stepDecayRateBps, settleToken));
    }

    function batchKick(KickParams[] calldata kicks) external onlyKeeperOrOwner {
        for (uint256 i = 0; i < kicks.length; i++) {
            _kick(kicks[i]);
        }
    }

    function sweepAndSettle(address auction, address sellToken) external onlyKeeperOrOwner {
        address receiver = IAuction(auction).receiver();
        uint256 sellAmount = IAuction(auction).available(sellToken);
        require(sellAmount != 0, "nothing to sweep");

        bytes[] memory state = new bytes[](3);
        state[0] = abi.encode(sellToken);
        state[1] = abi.encode(receiver);
        state[2] = abi.encode(sellAmount);

        bytes32[] memory commands = new bytes32[](3);
        commands[0] = WeiRollCommandLib.cmdCall(
            SWEEP_SELECTOR, 0, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, auction
        );
        commands[1] = WeiRollCommandLib.cmdCall(
            TRANSFER_SELECTOR, 1, 2, WeiRollCommandLib.ARG_UNUSED, sellToken
        );
        commands[2] = WeiRollCommandLib.cmdCall(
            SETTLE_SELECTOR, 0, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, auction
        );

        ITradeHandler(tradeHandler).execute(commands, state);
        emit SweepAndSettled(auction, sellToken);
    }

    function _kick(KickParams memory p) internal {
        require(p.startingPrice != 0, "starting price zero");
        require(IAuction(p.auction).want() == p.wantToken, "want mismatch");
        require(p.sellToken != p.wantToken, "sell token is want");
        require(IAuction(p.auction).receiver() == p.source, "receiver mismatch");

        bytes[] memory state = new bytes[](8);
        state[0] = abi.encode(p.source);
        state[1] = abi.encode(p.auction);
        state[2] = abi.encode(p.sellAmount);
        state[3] = abi.encode(p.startingPrice);
        state[4] = abi.encode(p.minimumPrice);
        state[5] = abi.encode(p.sellToken);
        state[6] = abi.encode(p.stepDecayRateBps);
        state[7] = abi.encode(p.settleToken);

        uint256 commandCount = p.settleToken == address(0) ? 5 : 6;
        bytes32[] memory commands = new bytes32[](commandCount);
        uint256 commandIndex = 0;

        if (p.settleToken != address(0)) {
            commands[commandIndex++] = WeiRollCommandLib.cmdCall(
                SETTLE_SELECTOR, 7, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
            );
        }

        commands[commandIndex++] = WeiRollCommandLib.cmdCall(TRANSFER_FROM_SELECTOR, 0, 1, 2, p.sellToken);
        commands[commandIndex++] = WeiRollCommandLib.cmdCall(
            SET_STARTING_PRICE_SELECTOR, 3, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );
        commands[commandIndex++] = WeiRollCommandLib.cmdCall(
            SET_MINIMUM_PRICE_SELECTOR, 4, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );
        commands[commandIndex++] = WeiRollCommandLib.cmdCall(
            SET_STEP_DECAY_RATE_SELECTOR, 6, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );
        commands[commandIndex++] = WeiRollCommandLib.cmdCall(
            KICK_SELECTOR, 5, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );

        ITradeHandler(tradeHandler).execute(commands, state);
        emit Kicked(
            p.source, p.auction, p.sellToken, p.sellAmount, p.startingPrice, p.minimumPrice, p.stepDecayRateBps, p.settleToken
        );
    }
}
