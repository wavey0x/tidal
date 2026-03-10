// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.20;

library WeiRollCommandLib {
    uint8 internal constant FLAG_CALL = 0x01;
    uint8 internal constant ARG_UNUSED = 0xFF;

    function cmdCall(bytes4 sel, uint8 a0, uint8 a1, uint8 a2, address target) internal pure returns (bytes32) {
        return pack(sel, FLAG_CALL, a0, a1, a2, ARG_UNUSED, ARG_UNUSED, ARG_UNUSED, ARG_UNUSED, target);
    }

    function pack(
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
