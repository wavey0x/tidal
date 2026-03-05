// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

interface IStrategy {
    function want() external view returns (address);
}
