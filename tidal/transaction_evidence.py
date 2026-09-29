"""Bounded, read-only chain evidence for a retained managed transaction.

Business outcomes are applied separately after verifying the exact retained
intent against a fresh receipt and its canonical block.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from hexbytes import HexBytes


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def rpc_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an RPC quantity")
    number = int(str(value), 16 if str(value).lower().startswith("0x") else 10)
    if number < 0:
        raise ValueError("Negative RPC quantity")
    return number


def _hash(value: object) -> bytes:
    if not isinstance(value, (str, bytes, bytearray)):
        raise ValueError("Missing hash")
    result = bytes(HexBytes(value))
    if len(result) != 32:
        raise ValueError("Invalid hash length")
    return result


def _address(value: object) -> str:
    from tidal.normalizers import normalize_address
    from tidal.errors import AddressNormalizationError

    try:
        return normalize_address(value)
    except AddressNormalizationError as exc:
        raise ValueError("Missing or invalid address") from exc


@dataclass(frozen=True)
class ChainEvidence:
    transaction: dict
    receipt: dict
    block: dict


def hydrate_legacy_identity(retained: Mapping[str, object], transaction: dict, *, chain_id: int) -> dict:
    """Fill missing legacy fields only from the exact retained transaction.

    Today's account nonce is never a source for historical identity. Any
    retained field disagreement stops hydration, including unsigned intent.
    """
    try:
        identity = {
            "tx_hash": "0x" + _hash(transaction["hash"]).hex(),
            "chain_id": rpc_int(transaction["chainId"]),
            "signer": _address(transaction["from"]), "nonce": rpc_int(transaction["nonce"]),
            "to_address": _address(transaction["to"]),
            "data": "0x" + bytes(HexBytes(transaction["input"])).hex(),
            "value": str(rpc_int(transaction["value"])),
        }
        if _hash(retained["tx_hash"]) != _hash(identity["tx_hash"]) or identity["chain_id"] != chain_id:
            raise EvidenceError("LEGACY_CONFLICT", "Historical transaction hash or chain differs from the retained evidence.")
        for field, observed in identity.items():
            value = retained.get(field)
            if value is None:
                continue
            if field in {"nonce", "chain_id"}:
                value = rpc_int(value)
            elif field == "value":
                value = str(rpc_int(value))
            elif field in {"signer", "to_address"}:
                value = _address(value)
            elif field in {"data", "tx_hash"}:
                value = "0x" + bytes(HexBytes(value)).hex()
            if value != observed:
                raise EvidenceError("LEGACY_CONFLICT", f"Historical transaction disagrees with retained {field}.")
        return identity
    except EvidenceError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise EvidenceError("INCOMPLETE_IDENTITY", "Exact historical transaction identity is unavailable.") from exc


def verify_evidence(
    retained: Mapping[str, object], *, transaction: dict, receipt: dict,
    block: dict, chain_id: int,
) -> ChainEvidence:
    """Require every identity/intent field; absent legacy evidence is explicit."""
    required = ("chain_id", "signer", "nonce", "tx_hash", "to_address", "data", "value")
    if any(retained.get(field) is None for field in required):
        raise EvidenceError("INCOMPLETE_IDENTITY", "Retained transaction identity or intent is incomplete.")
    try:
        intent_matches = (
            rpc_int(retained["chain_id"]) == chain_id == rpc_int(transaction["chainId"])
            and _hash(retained["tx_hash"]) == _hash(transaction["hash"]) == _hash(receipt["transactionHash"])
            and _address(retained["signer"]) == _address(transaction["from"]) == _address(receipt["from"])
            and rpc_int(retained["nonce"]) == rpc_int(transaction["nonce"])
            and _address(retained["to_address"]) == _address(transaction["to"]) == _address(receipt["to"])
            and bytes(HexBytes(retained["data"])) == bytes(HexBytes(transaction["input"]))
            and rpc_int(retained["value"]) == rpc_int(transaction["value"])
            and rpc_int(receipt["status"]) in (0, 1)
        )
        if not intent_matches:
            raise EvidenceError("INTENT_MISMATCH", "The mined transaction does not match its retained identity and intent.")
        block_number = rpc_int(receipt["blockNumber"])
        if (
            block_number != rpc_int(block["number"])
            or block_number != rpc_int(transaction["blockNumber"])
            or _hash(receipt["blockHash"]) != _hash(block["hash"])
            or _hash(transaction["blockHash"]) != _hash(block["hash"])
            or rpc_int(receipt["transactionIndex"]) != rpc_int(transaction["transactionIndex"])
        ):
            raise EvidenceError("NONCANONICAL_RECEIPT", "Receipt and transaction do not identify the canonical block.")
        rpc_int(block["timestamp"])
    except EvidenceError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise EvidenceError("INCOMPLETE_RPC_EVIDENCE", "RPC returned incomplete or malformed transaction evidence.") from exc
    return ChainEvidence(transaction, receipt, block)


async def observe_transaction(web3_client, retained: Mapping[str, object]) -> ChainEvidence:
    """Fetch fresh receipt, transaction and canonical block data.

    Transport/not-found exceptions propagate to the caller without changing
    durable state. No receipt supplied by a client or earlier run is accepted.
    """
    tx_hash = retained.get("tx_hash")
    if tx_hash is None:
        raise EvidenceError("INCOMPLETE_IDENTITY", "The retained attempt has no transaction hash.")
    chain_id = await web3_client.get_chain_id()
    receipt = await web3_client.get_transaction_receipt(str(tx_hash), timeout_seconds=2)
    transaction = await web3_client.get_transaction(str(tx_hash))
    try:
        block_number = rpc_int(receipt["blockNumber"])
    except (KeyError, ValueError, TypeError) as exc:
        raise EvidenceError("INCOMPLETE_RPC_EVIDENCE", "RPC returned a receipt without a valid block number.") from exc
    block = await web3_client.get_block(block_number)
    return verify_evidence(
        retained, transaction=transaction, receipt=receipt, block=block,
        chain_id=chain_id,
    )
