#!/usr/bin/env python3
"""One-off migration script for recreating legacy auctions on the new factory.

The script has two distinct phases:

1. Build and cache a migration plan by discovering Yearn strategies and matching
   them against auctions from the legacy factory.
2. Reuse that cached plan to simulate or submit deployments to the new factory.

The expensive discovery work is cached on disk so reruns do not need to repeat
it unless explicitly requested with ``--refresh-plan``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(REPO_ROOT))

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.exceptions import TransactionNotFound

from tidal.chain.contracts.abis import AUCTION_ABI, AUCTION_FACTORY_ABI, STRATEGY_ABI
from tidal.chain.contracts.multicall import MulticallClient, MulticallRequest
from tidal.chain.contracts.yearn import YearnCurveFactoryReader
from tidal.chain.web3_client import Web3Client
from tidal.config import load_settings
from tidal.constants import (
    YEARN_AUCTION_FACTORY_ADDRESS,
    YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS,
    YEARN_CURVE_FACTORY_ADDRESS,
    ZERO_ADDRESS,
)
from tidal.normalizers import normalize_address
from tidal.scanner.discovery import StrategyDiscoveryService
from tidal.transaction_service.signer import TransactionSigner

NEW_AUCTION_FACTORY_ADDRESS = "0xbA7FCb508c7195eE5AE823F37eE2c11D7ED52F8e"
DEFAULT_CACHE_PATH = REPO_ROOT / ".cache" / "auction_migration_plan.json"
DEFAULT_REPORT_PATH = SCRIPT_DIR / "auction_migration.json"
PLAN_VERSION = 1

MIGRATION_AUCTION_ABI = AUCTION_ABI + [
    {
        "inputs": [],
        "name": "startingPrice",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MIGRATION_FACTORY_ABI = AUCTION_FACTORY_ABI + [
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

_AUCTION_METADATA_FIELDS = ("governance", "want", "receiver", "startingPrice", "version")
_AUCTION_ADDRESS_FIELDS = frozenset({"governance", "want", "receiver"})


@dataclass(slots=True)
class AuctionSpec:
    address: str
    want: str | None = None
    receiver: str | None = None
    governance: str | None = None
    starting_price: int | None = None
    version: str | None = None


@dataclass(slots=True)
class MigrationEntry:
    strategy_address: str
    vault_address: str
    legacy_auction_address: str
    legacy_auction_version: str | None
    want: str
    receiver: str
    governance: str
    starting_price: str
    salt: str
    status: str = "planned"
    predicted_new_auction_address: str | None = None
    new_auction_address: str | None = None
    new_auction_version: str | None = None
    deploy_tx_hash: str | None = None
    deploy_block_number: int | None = None
    submitted_at: str | None = None
    deployment_source: str | None = None
    last_dry_run_at: str | None = None
    verified_at: str | None = None
    last_error: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MigrationEntry":
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recreate legacy Yearn auctions on the new factory with cached discovery.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Optional scanner YAML config path.")
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Migration plan cache path (default: {DEFAULT_CACHE_PATH}).",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Migration report path (default: {DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "--legacy-factory",
        default=YEARN_AUCTION_FACTORY_ADDRESS,
        help=f"Legacy factory address (default: {YEARN_AUCTION_FACTORY_ADDRESS}).",
    )
    parser.add_argument(
        "--new-factory",
        default=NEW_AUCTION_FACTORY_ADDRESS,
        help=f"New factory address (default: {NEW_AUCTION_FACTORY_ADDRESS}).",
    )
    parser.add_argument(
        "--required-governance",
        default=YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS,
        help=f"Required governance address (default: {YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS}).",
    )
    parser.add_argument(
        "--refresh-plan",
        action="store_true",
        help="Rebuild the cached strategy/legacy-auction migration plan from chain.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N entries after filtering out already-verified items.",
    )
    parser.add_argument(
        "--from-address",
        default=None,
        help="Optional caller address for dry-run eth_call when no keystore is configured.",
    )
    parser.add_argument(
        "--gas-price-gwei",
        type=float,
        default=None,
        help="Optional legacy gasPrice override for live deployment transactions.",
    )
    parser.add_argument(
        "--max-priority-fee-wei",
        type=int,
        default=1000,
        help="EIP-1559 maxPriorityFeePerGas for live deployments (default: 1000 wei).",
    )
    parser.add_argument(
        "--max-fee-multiplier",
        type=float,
        default=2.0,
        help="Multiplier applied to base fee when deriving maxFeePerGas (default: 2.0).",
    )
    parser.add_argument(
        "--gas-multiplier",
        type=float,
        default=1.2,
        help="Gas estimate multiplier for live deployment transactions (default: 1.2).",
    )
    parser.add_argument(
        "--receipt-timeout",
        type=int,
        default=300,
        help="Seconds to wait for a live deployment receipt before exiting (default: 300).",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="Simulate deployments only.")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="Broadcast deployment transactions.")
    parser.set_defaults(dry_run=True)
    return parser.parse_args()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_cache(cache_path: Path) -> dict[str, Any]:
    return json.loads(cache_path.read_text(encoding="utf-8"))


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def save_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = utcnow_iso()
    write_json_file(cache_path, payload)


def build_report(cache: dict[str, Any]) -> dict[str, Any]:
    entries = cache.get("entries", [])
    status_counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    report_rows = [
        {
            "strategyAddress": entry["strategy_address"],
            "vaultAddress": entry["vault_address"],
            "status": entry.get("status"),
            "legacyAuctionAddress": entry["legacy_auction_address"],
            "legacyAuctionVersion": entry.get("legacy_auction_version"),
            "newAuctionAddress": entry.get("new_auction_address"),
            "newAuctionVersion": entry.get("new_auction_version"),
            "predictedNewAuctionAddress": entry.get("predicted_new_auction_address"),
            "want": entry["want"],
            "receiver": entry["receiver"],
            "governance": entry["governance"],
            "startingPrice": entry["starting_price"],
            "salt": entry["salt"],
            "deployTxHash": entry.get("deploy_tx_hash"),
            "deployBlockNumber": entry.get("deploy_block_number"),
            "submittedAt": entry.get("submitted_at"),
            "verifiedAt": entry.get("verified_at"),
            "lastDryRunAt": entry.get("last_dry_run_at"),
            "deploymentSource": entry.get("deployment_source"),
            "lastError": entry.get("last_error"),
        }
        for entry in entries
    ]

    return {
        "generatedAt": utcnow_iso(),
        "planVersion": cache.get("plan_version"),
        "chainId": cache.get("chain_id"),
        "legacyFactory": cache.get("legacy_factory"),
        "newFactory": cache.get("new_factory"),
        "requiredGovernance": cache.get("required_governance"),
        "createdAt": cache.get("created_at"),
        "updatedAt": cache.get("updated_at"),
        "summary": {
            "strategyCount": cache.get("strategy_count"),
            "matchedCount": cache.get("matched_count"),
            "rowCount": len(report_rows),
            "statusCounts": status_counts,
        },
        "migrations": report_rows,
    }


def write_report(report_path: Path, cache: dict[str, Any]) -> None:
    report = build_report(cache)
    write_json_file(report_path, report)
    print(f"Wrote migration report to {report_path}")


def require_rpc_url(settings) -> str:
    if not settings.rpc_url:
        raise SystemExit("RPC_URL is required")
    return settings.rpc_url


def build_sync_web3(settings) -> Web3:
    return Web3(
        HTTPProvider(
            require_rpc_url(settings),
            request_kwargs={"timeout": settings.rpc_timeout_seconds},
        )
    )


def maybe_build_signer(settings, *, require_for_live: bool) -> TransactionSigner | None:
    keystore_path = settings.resolved_txn_keystore_path
    passphrase = settings.txn_keystore_passphrase

    if keystore_path and passphrase:
        return TransactionSigner(str(keystore_path), passphrase)

    if require_for_live:
        raise SystemExit("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required for --live")

    return None


def build_salt(strategy_address: str, legacy_auction_address: str) -> str:
    return Web3.solidity_keccak(
        ["string", "address", "address"],
        ["tidal.auction.migration.v1", to_checksum_address(strategy_address), to_checksum_address(legacy_auction_address)],
    ).hex()


def entry_param_key(entry: MigrationEntry) -> tuple[str, str, str, int]:
    return (
        entry.want,
        entry.receiver,
        entry.governance,
        int(entry.starting_price),
    )


def auction_param_key(spec: AuctionSpec) -> tuple[str, str, str, int] | None:
    if (
        spec.want is None
        or spec.receiver is None
        or spec.governance is None
        or spec.starting_price is None
    ):
        return None
    return (spec.want, spec.receiver, spec.governance, int(spec.starting_price))


async def read_strategy_wants_many(
    web3_client: Web3Client,
    multicall_client: MulticallClient | None,
    strategy_addresses: list[str],
    *,
    multicall_enabled: bool,
    batch_size: int,
) -> dict[str, str | None]:
    output = {strategy_address: None for strategy_address in strategy_addresses}
    if not strategy_addresses:
        return output

    if not multicall_enabled or multicall_client is None:
        for strategy_address in strategy_addresses:
            contract = web3_client.contract(strategy_address, STRATEGY_ABI)
            try:
                output[strategy_address] = normalize_address(await web3_client.call(contract.functions.want()))
            except Exception:  # noqa: BLE001
                output[strategy_address] = None
        return output

    requests: list[MulticallRequest] = []
    for strategy_address in strategy_addresses:
        contract = web3_client.contract(strategy_address, STRATEGY_ABI)
        fn = contract.functions.want()
        requests.append(
            MulticallRequest(
                target=strategy_address,
                call_data=bytes(HexBytes(fn._encode_transaction_data())),
                logical_key=(strategy_address,),
            )
        )

    results = await multicall_client.execute(requests, batch_size=batch_size, allow_failure=True)
    for result in results:
        if not result.success:
            continue
        strategy_address = result.logical_key[0]
        try:
            output[strategy_address] = normalize_address(abi_decode(["address"], result.return_data)[0])
        except Exception:  # noqa: BLE001
            output[strategy_address] = None

    return output


async def read_auction_specs_many(
    web3_client: Web3Client,
    multicall_client: MulticallClient | None,
    auction_addresses: list[str],
    *,
    multicall_enabled: bool,
    batch_size: int,
) -> dict[str, AuctionSpec]:
    output = {
        auction_address: AuctionSpec(address=auction_address)
        for auction_address in auction_addresses
    }
    if not auction_addresses:
        return output

    if not multicall_enabled or multicall_client is None:
        for auction_address in auction_addresses:
            contract = web3_client.contract(auction_address, MIGRATION_AUCTION_ABI)
            spec = output[auction_address]
            try:
                spec.want = normalize_address(await web3_client.call(contract.functions.want()))
            except Exception:  # noqa: BLE001
                pass
            try:
                spec.receiver = normalize_address(await web3_client.call(contract.functions.receiver()))
            except Exception:  # noqa: BLE001
                pass
            try:
                spec.governance = normalize_address(await web3_client.call(contract.functions.governance()))
            except Exception:  # noqa: BLE001
                pass
            try:
                spec.starting_price = int(await web3_client.call(contract.functions.startingPrice()))
            except Exception:  # noqa: BLE001
                pass
            try:
                version = await web3_client.call(contract.functions.version())
                spec.version = str(version).strip() or None
            except Exception:  # noqa: BLE001
                pass
        return output

    requests: list[MulticallRequest] = []
    for auction_address in auction_addresses:
        contract = web3_client.contract(auction_address, MIGRATION_AUCTION_ABI)
        for field_name in _AUCTION_METADATA_FIELDS:
            fn = getattr(contract.functions, field_name)()
            requests.append(
                MulticallRequest(
                    target=auction_address,
                    call_data=bytes(HexBytes(fn._encode_transaction_data())),
                    logical_key=(auction_address, field_name),
                )
            )

    results = await multicall_client.execute(requests, batch_size=batch_size, allow_failure=True)
    for result in results:
        if not result.success:
            continue
        auction_address = result.logical_key[0]
        field = result.logical_key[1]
        spec = output[auction_address]
        try:
            if field in _AUCTION_ADDRESS_FIELDS:
                value = normalize_address(abi_decode(["address"], result.return_data)[0])
            elif field == "startingPrice":
                value = int(abi_decode(["uint256"], result.return_data)[0])
            else:
                raw = abi_decode(["string"], result.return_data)[0]
                value = str(raw).strip() or None
        except Exception:  # noqa: BLE001
            continue

        if field == "startingPrice":
            spec.starting_price = value
        else:
            setattr(spec, field if field != "startingPrice" else "starting_price", value)

    return output


async def read_factory_auction_specs(
    web3_client: Web3Client,
    multicall_client: MulticallClient | None,
    factory_address: str,
    *,
    multicall_enabled: bool,
    batch_size: int,
) -> tuple[list[str], dict[str, AuctionSpec]]:
    contract = web3_client.contract(factory_address, AUCTION_FACTORY_ABI)
    result = await web3_client.call(contract.functions.getAllAuctions())
    auction_addresses = [normalize_address(address) for address in result]
    specs = await read_auction_specs_many(
        web3_client,
        multicall_client,
        auction_addresses,
        multicall_enabled=multicall_enabled,
        batch_size=batch_size,
    )
    return auction_addresses, specs


def build_existing_new_factory_index(
    auction_addresses: list[str],
    specs: dict[str, AuctionSpec],
    *,
    required_governance: str,
) -> dict[tuple[str, str, str, int], AuctionSpec]:
    index: dict[tuple[str, str, str, int], AuctionSpec] = {}
    for auction_address in auction_addresses:
        spec = specs.get(auction_address)
        if spec is None:
            continue
        if spec.governance != required_governance:
            continue
        if spec.want in {None, ZERO_ADDRESS}:
            continue
        if spec.receiver in {None, ZERO_ADDRESS}:
            continue
        if spec.starting_price is None:
            continue
        key = auction_param_key(spec)
        if key is None:
            continue
        index[key] = spec
    return index


async def build_plan_from_chain(
    settings,
    *,
    legacy_factory: str,
    new_factory: str,
    required_governance: str,
) -> dict[str, Any]:
    web3_client = Web3Client(
        settings.rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        retry_attempts=settings.rpc_retry_attempts,
    )
    multicall_client = MulticallClient(
        web3_client,
        settings.multicall_address,
        enabled=settings.multicall_enabled,
    )
    yearn_reader = YearnCurveFactoryReader(
        web3_client,
        YEARN_CURVE_FACTORY_ADDRESS,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_discovery_batch_calls=settings.multicall_discovery_batch_calls,
        multicall_overflow_queue_max=settings.multicall_overflow_queue_max,
    )
    discovery_service = StrategyDiscoveryService(
        yearn_reader,
        concurrency=settings.scan_concurrency,
    )

    discovered, vault_count, discovery_stats = await discovery_service.discover()
    strategy_addresses = sorted({normalize_address(item.strategy_address) for item in discovered})
    strategy_to_vault = {
        normalize_address(item.strategy_address): normalize_address(item.vault_address)
        for item in discovered
    }

    strategy_wants = await read_strategy_wants_many(
        web3_client,
        multicall_client,
        strategy_addresses,
        multicall_enabled=settings.multicall_enabled,
        batch_size=settings.multicall_auction_batch_calls,
    )

    legacy_auction_addresses, legacy_specs = await read_factory_auction_specs(
        web3_client,
        multicall_client,
        legacy_factory,
        multicall_enabled=settings.multicall_enabled,
        batch_size=settings.multicall_auction_batch_calls,
    )

    legacy_lookup: dict[tuple[str, str], AuctionSpec] = {}
    for auction_address in legacy_auction_addresses:
        spec = legacy_specs.get(auction_address)
        if spec is None:
            continue
        if spec.governance != required_governance:
            continue
        if spec.want in {None, ZERO_ADDRESS}:
            continue
        if spec.receiver in {None, ZERO_ADDRESS}:
            continue
        if spec.starting_price is None:
            continue
        legacy_lookup[(spec.want, spec.receiver)] = spec

    entries: list[dict[str, Any]] = []
    for strategy_address in strategy_addresses:
        strategy_want = strategy_wants.get(strategy_address)
        if strategy_want is None:
            continue
        spec = legacy_lookup.get((strategy_want, strategy_address))
        if spec is None:
            continue
        if spec.want is None or spec.receiver is None or spec.governance is None or spec.starting_price is None:
            continue

        entry = MigrationEntry(
            strategy_address=strategy_address,
            vault_address=strategy_to_vault[strategy_address],
            legacy_auction_address=spec.address,
            legacy_auction_version=spec.version,
            want=spec.want,
            receiver=spec.receiver,
            governance=spec.governance,
            starting_price=str(spec.starting_price),
            salt=build_salt(strategy_address, spec.address),
        )
        entries.append(entry.to_dict())

    entries.sort(key=lambda item: item["strategy_address"])
    return {
        "plan_version": PLAN_VERSION,
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
        "chain_id": settings.chain_id,
        "legacy_factory": legacy_factory,
        "new_factory": new_factory,
        "required_governance": required_governance,
        "vault_count": vault_count,
        "strategy_count": len(strategy_addresses),
        "matched_count": len(entries),
        "discovery_stats": discovery_stats,
        "entries": entries,
    }


async def load_or_build_plan(
    settings,
    *,
    cache_path: Path,
    refresh_plan: bool,
    legacy_factory: str,
    new_factory: str,
    required_governance: str,
) -> dict[str, Any]:
    if cache_path.is_file() and not refresh_plan:
        cache = load_cache(cache_path)
        if cache.get("plan_version") != PLAN_VERSION:
            raise SystemExit(
                f"Cached plan {cache_path} has version {cache.get('plan_version')}; rerun with --refresh-plan"
            )
        if normalize_address(cache["legacy_factory"]) != legacy_factory:
            raise SystemExit(f"Cached legacy factory does not match {legacy_factory}; rerun with --refresh-plan")
        if normalize_address(cache["new_factory"]) != new_factory:
            raise SystemExit(f"Cached new factory does not match {new_factory}; rerun with --refresh-plan")
        if normalize_address(cache["required_governance"]) != required_governance:
            raise SystemExit(
                f"Cached governance does not match {required_governance}; rerun with --refresh-plan"
            )
        print(f"Loaded cached migration plan from {cache_path} ({len(cache['entries'])} entries).")
        return cache

    print("Building migration plan from chain state.")
    cache = await build_plan_from_chain(
        settings,
        legacy_factory=legacy_factory,
        new_factory=new_factory,
        required_governance=required_governance,
    )
    save_cache(cache_path, cache)
    print(
        "Cached migration plan to "
        f"{cache_path} ({cache['matched_count']} matched strategies from {cache['strategy_count']} discovered)."
    )
    return cache


async def read_new_factory_index(
    settings,
    *,
    new_factory: str,
    required_governance: str,
) -> dict[tuple[str, str, str, int], AuctionSpec]:
    web3_client = Web3Client(
        settings.rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        retry_attempts=settings.rpc_retry_attempts,
    )
    multicall_client = MulticallClient(
        web3_client,
        settings.multicall_address,
        enabled=settings.multicall_enabled,
    )
    auction_addresses, specs = await read_factory_auction_specs(
        web3_client,
        multicall_client,
        new_factory,
        multicall_enabled=settings.multicall_enabled,
        batch_size=settings.multicall_auction_batch_calls,
    )
    return build_existing_new_factory_index(
        auction_addresses,
        specs,
        required_governance=required_governance,
    )


def read_auction_spec_sync(w3: Web3, auction_address: str) -> AuctionSpec:
    contract = w3.eth.contract(address=to_checksum_address(auction_address), abi=MIGRATION_AUCTION_ABI)
    spec = AuctionSpec(address=normalize_address(auction_address))
    spec.want = normalize_address(contract.functions.want().call())
    spec.receiver = normalize_address(contract.functions.receiver().call())
    spec.governance = normalize_address(contract.functions.governance().call())
    spec.starting_price = int(contract.functions.startingPrice().call())
    try:
        version = contract.functions.version().call()
        spec.version = str(version).strip() or None
    except Exception:  # noqa: BLE001
        spec.version = None
    return spec


def verify_entry_against_auction(w3: Web3, entry: MigrationEntry, auction_address: str) -> AuctionSpec:
    code = w3.eth.get_code(to_checksum_address(auction_address))
    if not code:
        raise ValueError(f"No code found at {auction_address}")

    spec = read_auction_spec_sync(w3, auction_address)
    mismatches: list[str] = []
    if spec.want != entry.want:
        mismatches.append(f"want {spec.want} != {entry.want}")
    if spec.receiver != entry.receiver:
        mismatches.append(f"receiver {spec.receiver} != {entry.receiver}")
    if spec.governance != entry.governance:
        mismatches.append(f"governance {spec.governance} != {entry.governance}")
    if spec.starting_price != int(entry.starting_price):
        mismatches.append(f"startingPrice {spec.starting_price} != {entry.starting_price}")
    if mismatches:
        raise ValueError("; ".join(mismatches))
    return spec


def get_sync_caller(signer: TransactionSigner | None, from_address: str | None) -> str | None:
    if signer is not None:
        return signer.checksum_address
    if from_address:
        return to_checksum_address(from_address)
    return None


def derive_fee_params(
    w3: Web3,
    *,
    gas_price_gwei: float | None,
    max_priority_fee_wei: int,
    max_fee_multiplier: float,
) -> dict[str, int]:
    if gas_price_gwei is not None:
        gas_price_wei = int(gas_price_gwei * 10**9)
        return {"gasPrice": gas_price_wei}

    latest_block = w3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        gas_price_wei = int(w3.eth.gas_price)
        return {"gasPrice": gas_price_wei}

    priority_fee_wei = max(0, int(max_priority_fee_wei))
    max_fee_wei = max(int(base_fee * max_fee_multiplier) + priority_fee_wei, priority_fee_wei + 1)
    return {
        "maxPriorityFeePerGas": priority_fee_wei,
        "maxFeePerGas": max_fee_wei,
    }


def simulate_deployment(
    factory_contract,
    entry: MigrationEntry,
    *,
    caller: str | None,
) -> str:
    kwargs: dict[str, Any] = {}
    if caller is not None:
        kwargs["from"] = caller
    predicted = factory_contract.functions.createNewAuction(
        to_checksum_address(entry.want),
        to_checksum_address(entry.receiver),
        to_checksum_address(entry.governance),
        int(entry.starting_price),
        HexBytes(entry.salt),
    ).call(kwargs)
    return normalize_address(predicted)


def finalize_verified_entry(entry: MigrationEntry, spec: AuctionSpec, auction_address: str, *, source: str) -> None:
    entry.new_auction_address = normalize_address(auction_address)
    entry.new_auction_version = spec.version
    entry.deployment_source = source
    entry.status = "verified"
    entry.last_error = None
    entry.verified_at = utcnow_iso()


def handle_submitted_entry(
    w3: Web3,
    entry: MigrationEntry,
    *,
    existing_index: dict[tuple[str, str, str, int], AuctionSpec],
) -> bool:
    if not entry.deploy_tx_hash:
        return False

    try:
        receipt = w3.eth.get_transaction_receipt(entry.deploy_tx_hash)
    except TransactionNotFound:
        print(f"Pending tx still not mined for {entry.strategy_address}: {entry.deploy_tx_hash}")
        return True

    if receipt.status != 1:
        entry.status = "error"
        entry.last_error = f"Deployment tx reverted: {entry.deploy_tx_hash}"
        print(f"Deployment reverted for {entry.strategy_address}: {entry.deploy_tx_hash}")
        return True

    auction_address = entry.new_auction_address or entry.predicted_new_auction_address
    if not auction_address:
        spec = existing_index.get(entry_param_key(entry))
        if spec is None:
            raise ValueError(f"Receipt confirmed but no matching new auction found for {entry.strategy_address}")
        auction_address = spec.address

    verified_spec = verify_entry_against_auction(w3, entry, auction_address)
    finalize_verified_entry(entry, verified_spec, auction_address, source=entry.deployment_source or "tx")
    entry.deploy_block_number = int(receipt.blockNumber)
    print(f"Verified deployed auction for {entry.strategy_address}: {auction_address}")
    return True


def deploy_live_entry(
    w3: Web3,
    signer: TransactionSigner,
    factory_contract,
    entry: MigrationEntry,
    *,
    gas_price_gwei: float | None,
    max_priority_fee_wei: int,
    max_fee_multiplier: float,
    gas_multiplier: float,
    persist_submitted_state,
) -> str:
    print(f"Preparing live deployment for {entry.strategy_address}")
    predicted = simulate_deployment(factory_contract, entry, caller=signer.checksum_address)
    entry.predicted_new_auction_address = predicted

    nonce = w3.eth.get_transaction_count(signer.checksum_address, "pending")
    fee_params = derive_fee_params(
        w3,
        gas_price_gwei=gas_price_gwei,
        max_priority_fee_wei=max_priority_fee_wei,
        max_fee_multiplier=max_fee_multiplier,
    )
    tx_params: dict[str, Any] = {
        "chainId": w3.eth.chain_id,
        "from": signer.checksum_address,
        "nonce": nonce,
    }
    tx_params.update(fee_params)

    print(
        f"Estimating gas for {entry.strategy_address} "
        f"(nonce={nonce}, predicted={predicted}, fees={fee_params})"
    )
    tx = factory_contract.functions.createNewAuction(
        to_checksum_address(entry.want),
        to_checksum_address(entry.receiver),
        to_checksum_address(entry.governance),
        int(entry.starting_price),
        HexBytes(entry.salt),
    ).build_transaction(tx_params)
    gas_estimate = int(w3.eth.estimate_gas(tx))
    tx["gas"] = max(gas_estimate, math.ceil(gas_estimate * gas_multiplier))

    print(f"Sending tx for {entry.strategy_address} with gas={tx['gas']}")
    signed = signer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed).hex()

    entry.new_auction_address = predicted
    entry.deploy_tx_hash = tx_hash
    entry.submitted_at = utcnow_iso()
    entry.deployment_source = "tx"
    entry.status = "submitted"
    entry.last_error = None
    persist_submitted_state()

    print(f"Submitted tx for {entry.strategy_address}: {tx_hash}")
    return tx_hash


def wait_for_receipt_with_progress(
    w3: Web3,
    tx_hash: str,
    *,
    strategy_address: str,
    timeout_seconds: int,
    poll_interval_seconds: int = 5,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_log_at = 0.0

    while True:
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for receipt for {strategy_address}: {tx_hash}"
                )
            if now - last_log_at >= 15:
                remaining = max(0, int(deadline - now))
                print(
                    f"Waiting for receipt for {strategy_address}: {tx_hash} "
                    f"({remaining}s remaining)"
                )
                last_log_at = now
            time.sleep(poll_interval_seconds)


def process_entries(
    settings,
    *,
    cache: dict[str, Any],
    cache_path: Path,
    dry_run: bool,
    from_address: str | None,
    gas_price_gwei: float | None,
    max_priority_fee_wei: int,
    max_fee_multiplier: float,
    gas_multiplier: float,
    receipt_timeout: int,
    limit: int | None,
    existing_index: dict[tuple[str, str, str, int], AuctionSpec],
) -> None:
    signer = maybe_build_signer(settings, require_for_live=not dry_run)
    caller = get_sync_caller(signer, from_address)
    w3 = build_sync_web3(settings)
    factory_contract = w3.eth.contract(
        address=to_checksum_address(cache["new_factory"]),
        abi=MIGRATION_FACTORY_ABI,
    )

    entries = [MigrationEntry.from_dict(item) for item in cache["entries"]]
    processed = 0

    def persist_entries() -> None:
        cache["entries"] = [item.to_dict() for item in entries]
        save_cache(cache_path, cache)

    for entry in entries:
        if entry.status == "verified":
            continue
        if limit is not None and processed >= limit:
            break

        try:
            if entry.status == "submitted":
                consumed = handle_submitted_entry(w3, entry, existing_index=existing_index)
                if consumed:
                    processed += 1
                    continue

            existing = existing_index.get(entry_param_key(entry))
            if existing is not None:
                verified_spec = verify_entry_against_auction(w3, entry, existing.address)
                finalize_verified_entry(entry, verified_spec, existing.address, source="existing")
                print(f"Found existing matching auction for {entry.strategy_address}: {existing.address}")
                processed += 1
                continue

            if dry_run:
                predicted = simulate_deployment(factory_contract, entry, caller=caller)
                entry.predicted_new_auction_address = predicted
                entry.last_dry_run_at = utcnow_iso()
                entry.last_error = None
                print(f"Dry-run ok for {entry.strategy_address}: would deploy {predicted}")
                processed += 1
                continue

            if signer is None:
                raise ValueError("Signer required for live deployment")

            tx_hash = deploy_live_entry(
                w3,
                signer,
                factory_contract,
                entry,
                gas_price_gwei=gas_price_gwei,
                max_priority_fee_wei=max_priority_fee_wei,
                max_fee_multiplier=max_fee_multiplier,
                gas_multiplier=gas_multiplier,
                persist_submitted_state=persist_entries,
            )
            receipt = wait_for_receipt_with_progress(
                w3,
                tx_hash,
                strategy_address=entry.strategy_address,
                timeout_seconds=receipt_timeout,
            )
            if receipt.status != 1:
                raise ValueError(f"Deployment tx reverted: {tx_hash}")

            entry.deploy_block_number = int(receipt.blockNumber)
            verified_spec = verify_entry_against_auction(
                w3,
                entry,
                entry.new_auction_address or entry.predicted_new_auction_address or "",
            )
            finalize_verified_entry(
                entry,
                verified_spec,
                entry.new_auction_address or entry.predicted_new_auction_address or verified_spec.address,
                source="tx",
            )
            print(
                f"Verified deployed auction for {entry.strategy_address}: "
                f"{entry.new_auction_address or entry.predicted_new_auction_address}"
            )
            existing_index[entry_param_key(entry)] = AuctionSpec(
                address=entry.new_auction_address or verified_spec.address,
                want=verified_spec.want,
                receiver=verified_spec.receiver,
                governance=verified_spec.governance,
                starting_price=verified_spec.starting_price,
                version=verified_spec.version,
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            entry.status = "error"
            entry.last_error = str(exc)
            print(f"Error for {entry.strategy_address}: {exc}")
            processed += 1
        finally:
            persist_entries()


async def async_main(args: argparse.Namespace) -> tuple[dict[str, Any], dict[tuple[str, str, str, int], AuctionSpec], Any]:
    settings = load_settings(args.config, mode="server")
    legacy_factory = normalize_address(args.legacy_factory)
    new_factory = normalize_address(args.new_factory)
    required_governance = normalize_address(args.required_governance)
    require_rpc_url(settings)

    cache = await load_or_build_plan(
        settings,
        cache_path=args.cache_file,
        refresh_plan=args.refresh_plan,
        legacy_factory=legacy_factory,
        new_factory=new_factory,
        required_governance=required_governance,
    )
    existing_index = await read_new_factory_index(
        settings,
        new_factory=new_factory,
        required_governance=required_governance,
    )
    print(f"New factory currently has {len(existing_index)} matching auctions indexed by params.")
    return cache, existing_index, settings


def main() -> None:
    args = parse_args()
    cache, existing_index, settings = asyncio.run(async_main(args))
    process_entries(
        settings,
        cache=cache,
        cache_path=args.cache_file,
        dry_run=args.dry_run,
        from_address=args.from_address,
        gas_price_gwei=args.gas_price_gwei,
        max_priority_fee_wei=args.max_priority_fee_wei,
        max_fee_multiplier=args.max_fee_multiplier,
        gas_multiplier=args.gas_multiplier,
        receipt_timeout=args.receipt_timeout,
        limit=args.limit,
        existing_index=existing_index,
    )
    write_report(args.report_file, cache)


if __name__ == "__main__":
    main()
