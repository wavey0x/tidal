// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

interface IAuction {
    function want() external view returns (address);
    function startingPrice() external view returns (uint256);
    function setStartingPrice(uint256 _startingPrice) external;
    function kick(address _from) external returns (uint256);
}
