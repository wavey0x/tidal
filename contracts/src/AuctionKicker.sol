// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {ITradeHandler} from "./interfaces/ITradeHandler.sol";
import {IAuction} from "./interfaces/IAuction.sol";
import {IStrategy} from "./interfaces/IStrategy.sol";
import {WeiRollCommandLib} from "./utils/WeiRollCommandLib.sol";

contract AuctionKicker {
    bytes4 internal constant TRANSFER_FROM_SELECTOR = bytes4(keccak256("transferFrom(address,address,uint256)"));
    bytes4 internal constant SET_STARTING_PRICE_SELECTOR = bytes4(keccak256("setStartingPrice(uint256)"));
    bytes4 internal constant KICK_SELECTOR = bytes4(keccak256("kick(address)"));
    address public constant tradeHandler = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;

    event OwnerUpdated(address indexed owner);
    event KeeperUpdated(address indexed account, bool allowed);
    event Kicked(
        address indexed strategy, address indexed auction, address sellToken, uint256 sellAmount, uint256 startingPrice
    );

    address public owner;
    mapping(address => bool) public keeper;

    constructor() {
        owner = msg.sender;
        emit OwnerUpdated(msg.sender);
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
        keeper[account] = allowed;
        emit KeeperUpdated(account, allowed);
    }

    function kick(address strategy, address auction, address sellToken, uint256 sellAmount, uint256 startingPrice)
        external
        onlyKeeperOrOwner
    {
        require(startingPrice != 0, "starting price zero");
        require(IAuction(auction).want() == IStrategy(strategy).want(), "want mismatch");

        bytes[] memory state = new bytes[](5);
        state[0] = abi.encode(strategy);
        state[1] = abi.encode(auction);
        state[2] = abi.encode(sellAmount);
        state[3] = abi.encode(startingPrice);
        state[4] = abi.encode(sellToken);

        bytes32[] memory commands = new bytes32[](3);
        commands[0] = WeiRollCommandLib.cmdCall(TRANSFER_FROM_SELECTOR, 0, 1, 2, sellToken);
        commands[1] = WeiRollCommandLib.cmdCall(
            SET_STARTING_PRICE_SELECTOR, 3, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, auction
        );
        commands[2] = WeiRollCommandLib.cmdCall(
            KICK_SELECTOR, 4, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, auction
        );

        ITradeHandler(tradeHandler).execute(commands, state);
        emit Kicked(strategy, auction, sellToken, sellAmount, startingPrice);
    }
}
