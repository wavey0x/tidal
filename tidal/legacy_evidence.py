"""Evidence adapter for the retired mainnet sweepAndSettle contract.

The wrapper's event proves completion but omits the recovered amount. Require
the exact call, matching auction close, and both equal ERC-20 transfer legs.
Only the native ledger calls this after exact finalized transaction validation.
"""
from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak
from hexbytes import HexBytes

from tidal.constants import TRUSTED_HISTORICAL_AUCTION_KICKERS, YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS


def legacy_sweep_evidence(transaction: dict, receipt: dict) -> tuple:
    from tidal.operation_reconciler import DecodedSweep

    if not transaction.get("legacy") or transaction.get("chain_id") != 1:
        return ()
    if str(transaction.get("operation")).replace("-", "_") != "sweep_and_settle":
        return ()
    target = str(transaction.get("to_address", "")).lower()
    if target not in TRUSTED_HISTORICAL_AUCTION_KICKERS:
        return ()
    try:
        calldata = bytes(HexBytes(transaction["data"]))
        if len(calldata) != 68 or calldata[:4] != keccak(text="sweepAndSettle(address,address)")[:4]:
            return ()
        auction, token = decode(["address", "address"], calldata[4:])
        logs = [(str(log["address"]).lower(), tuple(bytes(HexBytes(item)) for item in log["topics"]),
                 bytes(HexBytes(log["data"]))) for log in receipt["logs"] if not log.get("removed")]
        word = lambda address: bytes.fromhex(address[2:]).rjust(32, b"\0")
        completed = (target, (keccak(text="SweepAndSettled(address,address)"), word(auction), word(token)), b"")
        settled = (auction, (keccak(text="AuctionSettled(address)"), word(token)), b"")
        if logs.count(completed) != 1 or logs.count(settled) != 1:
            return ()
        handler = YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS
        transfer_topic = keccak(text="Transfer(address,address,uint256)")
        incoming = [(index, data) for index, (address, topics, data) in enumerate(logs)
                    if address == token and topics == (transfer_topic, word(auction), word(handler))]
        outgoing = [(index, data) for index, (address, topics, data) in enumerate(logs)
                    if address == token and len(topics) == 3 and topics[:2] == (transfer_topic, word(handler))]
        if len(incoming) != 1 or len(outgoing) != 1:
            return ()
        before, recovered = incoming[0]
        after, delivered = outgoing[0]
        if not before < after < logs.index(settled) < logs.index(completed):
            return ()
        if len(recovered) != 32 or recovered != delivered or int.from_bytes(recovered) == 0:
            return ()
        return (DecodedSweep(auction, token, int.from_bytes(recovered)),)
    except (ValueError, TypeError, KeyError, DecodingError):
        return ()
