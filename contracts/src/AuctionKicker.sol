// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {ITradeHandler} from "./interfaces/ITradeHandler.sol";
import {IAuction} from "./interfaces/IAuction.sol";
import {WeiRollCommandLib} from "./utils/WeiRollCommandLib.sol";

contract AuctionKicker {
    bytes4 internal constant TRANSFER_FROM_SELECTOR = bytes4(keccak256("transferFrom(address,address,uint256)"));
    bytes4 internal constant SET_STARTING_PRICE_SELECTOR = bytes4(keccak256("setStartingPrice(uint256)"));
    bytes4 internal constant SET_MINIMUM_PRICE_SELECTOR = bytes4(keccak256("setMinimumPrice(uint256)"));
    bytes4 internal constant KICK_SELECTOR = bytes4(keccak256("kick(address)"));
    address public constant tradeHandler = 0xb634316E06cC0B358437CbadD4dC94F1D3a92B3b;

    event OwnerUpdated(address indexed owner);
    event KeeperUpdated(address indexed account, bool allowed);
    event Kicked(
        address indexed source, address indexed auction, address sellToken, uint256 sellAmount, uint256 startingPrice, uint256 minimumPrice
    );

    struct KickParams {
        address source;
        address auction;
        address sellToken;
        uint256 sellAmount;
        address wantToken;
        uint256 startingPrice;
        uint256 minimumPrice;
    }

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

    function kick(address source, address auction, address sellToken, uint256 sellAmount, address wantToken, uint256 startingPrice, uint256 minimumPrice)
        external
        onlyKeeperOrOwner
    {
        _kick(KickParams(source, auction, sellToken, sellAmount, wantToken, startingPrice, minimumPrice));
    }

    function batchKick(KickParams[] calldata kicks) external onlyKeeperOrOwner {
        for (uint256 i = 0; i < kicks.length; i++) {
            _kick(kicks[i]);
        }
    }

    function _kick(KickParams memory p) internal {
        require(p.startingPrice != 0, "starting price zero");
        require(IAuction(p.auction).want() == p.wantToken, "want mismatch");
        require(IAuction(p.auction).receiver() == p.source, "receiver mismatch");

        bytes[] memory state = new bytes[](6);
        state[0] = abi.encode(p.source);
        state[1] = abi.encode(p.auction);
        state[2] = abi.encode(p.sellAmount);
        state[3] = abi.encode(p.startingPrice);
        state[4] = abi.encode(p.minimumPrice);
        state[5] = abi.encode(p.sellToken);

        bytes32[] memory commands = new bytes32[](4);
        commands[0] = WeiRollCommandLib.cmdCall(TRANSFER_FROM_SELECTOR, 0, 1, 2, p.sellToken);
        commands[1] = WeiRollCommandLib.cmdCall(
            SET_STARTING_PRICE_SELECTOR, 3, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );
        commands[2] = WeiRollCommandLib.cmdCall(
            SET_MINIMUM_PRICE_SELECTOR, 4, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );
        commands[3] = WeiRollCommandLib.cmdCall(
            KICK_SELECTOR, 5, WeiRollCommandLib.ARG_UNUSED, WeiRollCommandLib.ARG_UNUSED, p.auction
        );

        ITradeHandler(tradeHandler).execute(commands, state);
        emit Kicked(p.source, p.auction, p.sellToken, p.sellAmount, p.startingPrice, p.minimumPrice);
    }
}
