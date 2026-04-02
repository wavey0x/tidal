"""Auction deployment helpers for the main CLI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import Web3

from tidal.chain.contracts.abis import AUCTION_ABI, AUCTION_FACTORY_ABI, ERC20_ABI, MULTICALL3_ABI
from tidal.constants import YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS
from tidal.normalizers import normalize_address, short_address
from tidal.time import utcnow_iso
from tidal.transaction_service.signer import TransactionSigner

NEW_AUCTION_FACTORY_ADDRESS = "0xbA7FCb508c7195eE5AE823F37eE2c11D7ED52F8e"

SINGLE_AUCTION_FACTORY_ABI = AUCTION_FACTORY_ABI + [
    {
        "inputs": [
            {"internalType": "address", "name": "_want", "type": "address"},
            {"internalType": "address", "name": "_receiver", "type": "address"},
            {"internalType": "address", "name": "_governance", "type": "address"},
            {"internalType": "uint256", "name": "_startingPrice", "type": "uint256"},
            {"internalType": "bytes32", "name": "_salt", "type": "bytes32"},
        ],
        "name": "createNewAuction",
        "outputs": [{"internalType": "address", "name": "newAuction", "type": "address"}],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

SINGLE_AUCTION_ABI = AUCTION_ABI


@dataclass(slots=True)
class ExistingAuctionMatch:
    factory_address: str
    auction_address: str
    want: str
    receiver: str
    governance: str
    starting_price: int | None
    version: str | None


@dataclass(slots=True)
class AuctionDeployPreview:
    factory_address: str
    want: str
    receiver: str
    governance: str
    starting_price: int
    salt: str
    sender_address: str | None
    existing_matches: list[ExistingAuctionMatch]
    predicted_address: str | None
    predicted_address_exists: bool
    gas_estimate: int | None
    preview_error: str | None
    gas_error: str | None


@dataclass(slots=True)
class AuctionDeployExecution:
    tx_hash: str
    broadcast_at: str
    receipt_status: int
    block_number: int | None
    gas_used: int | None


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_default_salt(want: str, receiver: str, governance: str) -> str:
    payload = (
        f"tidal.manual.auction.v1:{normalize_address(want)}:{normalize_address(receiver)}:"
        f"{normalize_address(governance)}:{int(time.time())}"
    )
    return Web3.keccak(text=payload).hex()


def read_token_symbol(w3: Web3, token_address: str) -> str | None:
    token = w3.eth.contract(address=to_checksum_address(token_address), abi=ERC20_ABI)
    try:
        symbol = str(token.functions.symbol().call()).strip()
    except Exception:  # noqa: BLE001
        return None
    return symbol or None


def read_factory_auction_addresses(w3: Web3, factory_address: str) -> list[str]:
    factory = w3.eth.contract(address=to_checksum_address(factory_address), abi=AUCTION_FACTORY_ABI)
    auction_addresses = factory.functions.getAllAuctions().call()
    return [normalize_address(address) for address in auction_addresses]


def _decode_auction_field(field_name: str, return_data: bytes) -> Any:
    if field_name in {"want", "receiver", "governance"}:
        return normalize_address(abi_decode(["address"], return_data)[0])
    if field_name == "startingPrice":
        return int(abi_decode(["uint256"], return_data)[0])
    if field_name == "version":
        raw = abi_decode(["string"], return_data)[0]
        return str(raw).strip() or None
    raise ValueError(f"Unsupported field: {field_name}")


def read_auction_fields_many(
    w3: Web3,
    settings: Any,
    *,
    auction_addresses: list[str],
    field_names: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    output = {auction_address: {} for auction_address in auction_addresses}
    if not auction_addresses:
        return output

    multicall_address = getattr(settings, "multicall_address", None)
    multicall_enabled = bool(getattr(settings, "multicall_enabled", False) and multicall_address)
    batch_size = max(int(getattr(settings, "multicall_auction_batch_calls", 100)), 1)

    if multicall_enabled:
        multicall = w3.eth.contract(address=to_checksum_address(multicall_address), abi=MULTICALL3_ABI)
        requests: list[dict[str, Any]] = []
        logical_keys: list[tuple[str, str]] = []
        for auction_address in auction_addresses:
            contract = w3.eth.contract(address=to_checksum_address(auction_address), abi=SINGLE_AUCTION_ABI)
            for field_name in field_names:
                fn = getattr(contract.functions, field_name)()
                requests.append(
                    {
                        "target": to_checksum_address(auction_address),
                        "allowFailure": True,
                        "callData": HexBytes(fn._encode_transaction_data()),
                    }
                )
                logical_keys.append((auction_address, field_name))

        try:
            for request_chunk, key_chunk in zip(
                chunked(requests, batch_size),
                chunked(logical_keys, batch_size),
                strict=True,
            ):
                raw_results = multicall.functions.aggregate3(request_chunk).call()
                for logical_key, raw in zip(key_chunk, raw_results, strict=True):
                    if isinstance(raw, dict):
                        success = bool(raw["success"])
                        return_data = bytes(raw["returnData"])
                    else:
                        success = bool(raw[0])
                        return_data = bytes(raw[1])
                    if not success:
                        continue
                    auction_address, field_name = logical_key
                    try:
                        output[auction_address][field_name] = _decode_auction_field(field_name, return_data)
                    except Exception:
                        continue
            return output
        except Exception:
            pass

    for auction_address in auction_addresses:
        contract = w3.eth.contract(address=to_checksum_address(auction_address), abi=SINGLE_AUCTION_ABI)
        for field_name in field_names:
            try:
                value = getattr(contract.functions, field_name)().call()
                if field_name in {"want", "receiver", "governance"}:
                    output[auction_address][field_name] = normalize_address(value)
                elif field_name == "startingPrice":
                    output[auction_address][field_name] = int(value)
                elif field_name == "version":
                    output[auction_address][field_name] = str(value).strip() or None
            except Exception:
                continue

    return output


def read_existing_matches(
    w3: Web3,
    settings: Any,
    *,
    factory_address: str,
    auction_addresses: list[str],
    want: str,
    receiver: str,
    governance: str,
) -> list[ExistingAuctionMatch]:
    want = normalize_address(want)
    receiver = normalize_address(receiver)
    governance = normalize_address(governance)
    identity_fields = read_auction_fields_many(
        w3,
        settings,
        auction_addresses=auction_addresses,
        field_names=("want", "receiver", "governance"),
    )
    matching_addresses = [
        auction_address
        for auction_address in auction_addresses
        if identity_fields.get(auction_address, {}).get("want") == want
        and identity_fields.get(auction_address, {}).get("receiver") == receiver
        and identity_fields.get(auction_address, {}).get("governance") == governance
    ]
    if not matching_addresses:
        return []

    detail_fields = read_auction_fields_many(
        w3,
        settings,
        auction_addresses=matching_addresses,
        field_names=("startingPrice", "version"),
    )
    return [
        ExistingAuctionMatch(
            factory_address=factory_address,
            auction_address=auction_address,
            want=want,
            receiver=receiver,
            governance=governance,
            starting_price=detail_fields.get(auction_address, {}).get("startingPrice"),
            version=detail_fields.get(auction_address, {}).get("version"),
        )
        for auction_address in matching_addresses
    ]


def derive_fee_settings(w3: Web3) -> tuple[int, int]:
    latest_block = w3.eth.get_block("latest")
    base_fee = int(latest_block.get("baseFeePerGas") or 0)
    try:
        priority_fee = int(w3.eth.max_priority_fee)
    except Exception:  # noqa: BLE001
        priority_fee = w3.to_wei(1, "gwei")

    if base_fee > 0:
        max_fee = int(base_fee * 2 + priority_fee)
    else:
        gas_price = int(w3.eth.gas_price)
        max_fee = gas_price
        priority_fee = 0
    return max_fee, priority_fee


def default_factory_address(settings: Any) -> str:
    return normalize_address(settings.auction_factory_address or NEW_AUCTION_FACTORY_ADDRESS)


def default_governance_address() -> str:
    return normalize_address(YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS)


def resolve_starting_price(*, provided: int | None, matches: list[ExistingAuctionMatch]) -> int:
    if provided is not None:
        return provided
    for match in matches:
        if match.starting_price is not None:
            return int(match.starting_price)
    raise ValueError("starting price is required when no matching auction provides a default")


def preview_deployment(
    w3: Web3,
    settings: Any,
    *,
    factory_address: str,
    want: str,
    receiver: str,
    governance: str,
    starting_price: int,
    salt: str,
    sender_address: str | None,
) -> AuctionDeployPreview:
    existing_auctions = read_factory_auction_addresses(w3, factory_address)
    existing_matches = read_existing_matches(
        w3,
        settings,
        factory_address=factory_address,
        auction_addresses=existing_auctions,
        want=want,
        receiver=receiver,
        governance=governance,
    )

    factory = w3.eth.contract(address=to_checksum_address(factory_address), abi=SINGLE_AUCTION_FACTORY_ABI)
    create_fn = factory.functions.createNewAuction(
        to_checksum_address(want),
        to_checksum_address(receiver),
        to_checksum_address(governance),
        starting_price,
        HexBytes(salt),
    )
    call_kwargs = {"from": to_checksum_address(sender_address)} if sender_address else {}

    predicted_address: str | None = None
    gas_estimate: int | None = None
    preview_error: str | None = None
    gas_error: str | None = None

    try:
        predicted_address = normalize_address(create_fn.call(call_kwargs))
    except Exception as exc:  # noqa: BLE001
        preview_error = str(exc)

    try:
        tx = create_fn.build_transaction(call_kwargs)
        gas_estimate = int(w3.eth.estimate_gas(tx))
    except Exception as exc:  # noqa: BLE001
        gas_error = str(exc)

    return AuctionDeployPreview(
        factory_address=factory_address,
        want=want,
        receiver=receiver,
        governance=governance,
        starting_price=starting_price,
        salt=salt,
        sender_address=sender_address,
        existing_matches=existing_matches,
        predicted_address=predicted_address,
        predicted_address_exists=predicted_address in existing_auctions if predicted_address is not None else False,
        gas_estimate=gas_estimate,
        preview_error=preview_error,
        gas_error=gas_error,
    )


def send_live_deployment(
    w3: Web3,
    *,
    signer: TransactionSigner,
    factory_address: str,
    want: str,
    receiver: str,
    governance: str,
    starting_price: int,
    salt: str,
    receipt_timeout: int = 300,
) -> AuctionDeployExecution:
    factory = w3.eth.contract(address=to_checksum_address(factory_address), abi=SINGLE_AUCTION_FACTORY_ABI)
    create_fn = factory.functions.createNewAuction(
        to_checksum_address(want),
        to_checksum_address(receiver),
        to_checksum_address(governance),
        starting_price,
        HexBytes(salt),
    )

    max_fee, priority_fee = derive_fee_settings(w3)
    nonce = int(w3.eth.get_transaction_count(signer.checksum_address, "pending"))
    tx = create_fn.build_transaction(
        {
            "from": signer.checksum_address,
            "chainId": int(w3.eth.chain_id),
            "nonce": nonce,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
        }
    )
    gas_estimate = int(w3.eth.estimate_gas(tx))
    tx["gas"] = int(gas_estimate * 1.2)

    signed_tx = signer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx).hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    broadcast_at = utcnow_iso()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=receipt_timeout)
    return AuctionDeployExecution(
        tx_hash=tx_hash,
        broadcast_at=broadcast_at,
        receipt_status=int(receipt["status"]),
        block_number=receipt.get("blockNumber"),
        gas_used=receipt.get("gasUsed"),
    )


def summarize_matches(matches: list[ExistingAuctionMatch]) -> list[str]:
    if not matches:
        return ["No existing auction match found in the selected factory."]
    return [
        (
            f"auction={short_address(match.auction_address)} "
            f"factory={short_address(match.factory_address)} "
            f"startingPrice={match.starting_price if match.starting_price is not None else 'unknown'} "
            f"version={match.version or 'unknown'}"
        )
        for match in matches
    ]
