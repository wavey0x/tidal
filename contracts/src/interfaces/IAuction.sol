// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

interface IAuction {
    function governance() external view returns (address);
    function want() external view returns (address);
    function receiver() external view returns (address);
    function getAllEnabledAuctions() external view returns (address[] memory);
    function startingPrice() external view returns (uint256);
    function setStartingPrice(uint256 _startingPrice) external;
    function minimumPrice() external view returns (uint256);
    function setMinimumPrice(uint256 _minimumPrice) external;
    function stepDecayRate() external view returns (uint256);
    function setStepDecayRate(uint256 _stepDecayRate) external;
    function price(address _from) external view returns (uint256);
    function available(address _from) external view returns (uint256);
    function isActive(address _from) external view returns (bool);
    function settle(address _from) external;
    function sweep(address _token) external;
    function enable(address _from) external;
    function kick(address _from) external returns (uint256);
}
