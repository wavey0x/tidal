// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

interface ITradeHandler {
    function execute(bytes32[] calldata commands, bytes[] calldata state) external returns (bytes[] memory);
    function addMech(address mech) external;
    function governance() external view returns (address);
    function mechs(address mech) external view returns (bool);
}
