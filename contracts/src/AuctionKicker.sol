// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

import {ITradeHandler} from "./interfaces/ITradeHandler.sol";
import {IAuction} from "./interfaces/IAuction.sol";
import {IStrategy} from "./interfaces/IStrategy.sol";

contract AuctionKicker {
    uint8 internal constant FLAG_CALL = 0x01;
    uint8 internal constant ARG_UNUSED = 0xFF;

    bytes4 internal constant TRANSFER_FROM_SELECTOR = bytes4(keccak256("transferFrom(address,address,uint256)"));
    bytes4 internal constant SET_STARTING_PRICE_SELECTOR = bytes4(keccak256("setStartingPrice(uint256)"));
    bytes4 internal constant KICK_SELECTOR = bytes4(keccak256("kick(address)"));

    address public immutable tradeHandler;

    address public owner;
    mapping(address => bool) public keeper;

    error Unauthorized();
    error ZeroAddress();
    error StartingPriceZero();
    error WantMismatch();

    constructor(address _tradeHandler) {
        if (_tradeHandler == address(0)) revert ZeroAddress();
        tradeHandler = _tradeHandler;
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier onlyKeeperOrOwner() {
        if (msg.sender != owner && !keeper[msg.sender]) revert Unauthorized();
        _;
    }

    function setOwner(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        owner = newOwner;
    }

    function setKeeper(address account, bool allowed) external onlyOwner {
        keeper[account] = allowed;
    }

    function kick(address strategy, address auction, address sellToken, uint256 sellAmount, uint256 startingPrice)
        external
        onlyKeeperOrOwner
    {
        if (startingPrice == 0) revert StartingPriceZero();
        if (IAuction(auction).want() != IStrategy(strategy).want()) revert WantMismatch();

        bytes[] memory state = new bytes[](5);
        state[0] = abi.encode(strategy);
        state[1] = abi.encode(auction);
        state[2] = abi.encode(sellAmount);
        state[3] = abi.encode(startingPrice);
        state[4] = abi.encode(sellToken);

        bytes32[] memory commands = new bytes32[](3);
        commands[0] = _cmdCall(TRANSFER_FROM_SELECTOR, 0, 1, 2, sellToken);
        commands[1] = _cmdCall(SET_STARTING_PRICE_SELECTOR, 3, ARG_UNUSED, ARG_UNUSED, auction);
        commands[2] = _cmdCall(KICK_SELECTOR, 4, ARG_UNUSED, ARG_UNUSED, auction);

        ITradeHandler(tradeHandler).execute(commands, state);
    }

    function _cmdCall(bytes4 sel, uint8 a0, uint8 a1, uint8 a2, address target) internal pure returns (bytes32) {
        return _pack(sel, FLAG_CALL, a0, a1, a2, ARG_UNUSED, ARG_UNUSED, ARG_UNUSED, ARG_UNUSED, target);
    }

    function _pack(
        bytes4 sel,
        uint8 flags,
        uint8 a0,
        uint8 a1,
        uint8 a2,
        uint8 a3,
        uint8 a4,
        uint8 a5,
        uint8 out,
        address target
    ) internal pure returns (bytes32) {
        uint256 command = uint256(uint32(sel)) << 224;
        command |= uint256(flags) << 216;
        command |= uint256(a0) << 208;
        command |= uint256(a1) << 200;
        command |= uint256(a2) << 192;
        command |= uint256(a3) << 184;
        command |= uint256(a4) << 176;
        command |= uint256(a5) << 168;
        command |= uint256(out) << 160;
        command |= uint256(uint160(target));
        return bytes32(command);
    }
}
