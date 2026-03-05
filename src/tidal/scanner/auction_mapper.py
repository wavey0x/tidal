"""Strategy-to-auction mapping refresh and JSON cache persistence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from eth_abi import decode as abi_decode
from hexbytes import HexBytes

from tidal.chain.contracts.abis import AUCTION_ABI, AUCTION_FACTORY_ABI, STRATEGY_ABI
from tidal.chain.contracts.multicall import MulticallClient, MulticallRequest
from tidal.constants import ZERO_ADDRESS
from tidal.normalizers import normalize_address
from tidal.time import utcnow_iso


@dataclass(slots=True)
class AuctionMappingRefreshResult:
    strategy_to_auction: dict[str, str | None]
    auction_count: int
    governance_allowed_auction_count: int
    mapped_count: int
    unmapped_count: int
    source: str


class StrategyAuctionMapper:
    """Builds strategy->auction mappings and stores them in a JSON snapshot."""

    def __init__(
        self,
        *,
        web3_client,
        chain_id: int,
        auction_factory_address: str,
        required_governance_address: str,
        cache_path: Path,
        multicall_client: MulticallClient | None = None,
        multicall_enabled: bool = True,
        multicall_auction_batch_calls: int = 500,
    ) -> None:
        self.web3_client = web3_client
        self.chain_id = chain_id
        self.auction_factory_address = normalize_address(auction_factory_address)
        self.required_governance_address = normalize_address(required_governance_address)
        self.cache_path = cache_path
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_auction_batch_calls = multicall_auction_batch_calls

    async def refresh_for_strategies(self, strategy_addresses: list[str]) -> AuctionMappingRefreshResult:
        normalized_strategies = sorted({normalize_address(address) for address in strategy_addresses})

        want_to_auction: dict[str, str] = {}
        auction_addresses = await self._read_auction_addresses()
        auction_metadata = await self._read_auction_metadata_many(auction_addresses)

        governance_allowed_auction_count = 0
        for auction_address in auction_addresses:
            entry = auction_metadata.get(auction_address, {})
            governance_address = entry.get("governance")
            want_address = entry.get("want")
            if governance_address != self.required_governance_address:
                continue
            if want_address is None or want_address == ZERO_ADDRESS:
                continue
            governance_allowed_auction_count += 1
            # Last address wins to match the "newest by factory order" rule.
            want_to_auction[want_address] = auction_address

        strategy_to_auction: dict[str, str | None] = {}
        strategy_wants = await self._read_strategy_wants_many(normalized_strategies)
        for strategy_address in normalized_strategies:
            strategy_want = strategy_wants.get(strategy_address)
            strategy_to_auction[strategy_address] = want_to_auction.get(strategy_want)

        mapped_count = sum(1 for auction_address in strategy_to_auction.values() if auction_address)
        unmapped_count = len(strategy_to_auction) - mapped_count

        payload = {
            "version": 1,
            "chainId": self.chain_id,
            "factoryAddress": self.auction_factory_address,
            "requiredGovernanceAddress": self.required_governance_address,
            "updatedAt": utcnow_iso(),
            "selectionRule": "latest_by_factory_order",
            "strategyToAuction": strategy_to_auction,
        }
        self._write_cache_payload(payload)

        return AuctionMappingRefreshResult(
            strategy_to_auction=strategy_to_auction,
            auction_count=len(auction_addresses),
            governance_allowed_auction_count=governance_allowed_auction_count,
            mapped_count=mapped_count,
            unmapped_count=unmapped_count,
            source="fresh",
        )

    def load_cached_mapping(self) -> dict[str, str | None]:
        payload = self._read_cache_payload()
        raw_mapping = payload.get("strategyToAuction", {})
        if not isinstance(raw_mapping, dict):
            return {}

        output: dict[str, str | None] = {}
        for strategy_address, auction_address in raw_mapping.items():
            try:
                normalized_strategy = normalize_address(str(strategy_address))
            except Exception:  # noqa: BLE001
                continue

            if auction_address is None:
                output[normalized_strategy] = None
                continue

            try:
                output[normalized_strategy] = normalize_address(str(auction_address))
            except Exception:  # noqa: BLE001
                output[normalized_strategy] = None

        return output

    async def _read_auction_addresses(self) -> list[str]:
        factory = self.web3_client.contract(self.auction_factory_address, AUCTION_FACTORY_ABI)
        result = await self.web3_client.call(factory.functions.getAllAuctions())
        return [normalize_address(address) for address in result]

    async def _read_auction_metadata_many(self, auction_addresses: list[str]) -> dict[str, dict[str, str | None]]:
        output = {
            auction_address: {
                "governance": None,
                "want": None,
            }
            for auction_address in auction_addresses
        }
        if not auction_addresses:
            return output

        if not self.multicall_enabled or self.multicall_client is None:
            for auction_address in auction_addresses:
                auction_contract = self.web3_client.contract(auction_address, AUCTION_ABI)
                try:
                    output[auction_address]["governance"] = normalize_address(
                        await self.web3_client.call(auction_contract.functions.governance())
                    )
                except Exception:  # noqa: BLE001
                    output[auction_address]["governance"] = None

                try:
                    output[auction_address]["want"] = normalize_address(
                        await self.web3_client.call(auction_contract.functions.want())
                    )
                except Exception:  # noqa: BLE001
                    output[auction_address]["want"] = None
            return output

        requests: list[MulticallRequest] = []
        for auction_address in auction_addresses:
            auction_contract = self.web3_client.contract(auction_address, AUCTION_ABI)
            governance_fn = auction_contract.functions.governance()
            want_fn = auction_contract.functions.want()
            requests.append(
                MulticallRequest(
                    target=auction_address,
                    call_data=bytes(HexBytes(governance_fn._encode_transaction_data())),
                    logical_key=(auction_address, "governance"),
                )
            )
            requests.append(
                MulticallRequest(
                    target=auction_address,
                    call_data=bytes(HexBytes(want_fn._encode_transaction_data())),
                    logical_key=(auction_address, "want"),
                )
            )

        multicall_results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_auction_batch_calls,
            allow_failure=True,
        )

        for result in multicall_results:
            auction_address = result.logical_key[0]
            field = result.logical_key[1]
            if not result.success:
                continue
            try:
                decoded = normalize_address(abi_decode(["address"], result.return_data)[0])
            except Exception:  # noqa: BLE001
                continue
            output[auction_address][field] = decoded

        return output

    async def _read_strategy_wants_many(self, strategy_addresses: list[str]) -> dict[str, str | None]:
        output = {strategy_address: None for strategy_address in strategy_addresses}
        if not strategy_addresses:
            return output

        if not self.multicall_enabled or self.multicall_client is None:
            for strategy_address in strategy_addresses:
                strategy_contract = self.web3_client.contract(strategy_address, STRATEGY_ABI)
                try:
                    output[strategy_address] = normalize_address(
                        await self.web3_client.call(strategy_contract.functions.want())
                    )
                except Exception:  # noqa: BLE001
                    output[strategy_address] = None
            return output

        requests: list[MulticallRequest] = []
        for strategy_address in strategy_addresses:
            strategy_contract = self.web3_client.contract(strategy_address, STRATEGY_ABI)
            want_fn = strategy_contract.functions.want()
            requests.append(
                MulticallRequest(
                    target=strategy_address,
                    call_data=bytes(HexBytes(want_fn._encode_transaction_data())),
                    logical_key=(strategy_address,),
                )
            )

        multicall_results = await self.multicall_client.execute(
            requests,
            batch_size=self.multicall_auction_batch_calls,
            allow_failure=True,
        )
        for result in multicall_results:
            strategy_address = result.logical_key[0]
            if not result.success:
                continue
            try:
                output[strategy_address] = normalize_address(abi_decode(["address"], result.return_data)[0])
            except Exception:  # noqa: BLE001
                output[strategy_address] = None

        return output

    def _read_cache_payload(self) -> dict[str, object]:
        if not self.cache_path.exists():
            return {}

        try:
            with self.cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:  # noqa: BLE001
            return {}

        return payload if isinstance(payload, dict) else {}

    def _write_cache_payload(self, payload: dict[str, object]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cache_path.parent,
                prefix=f".{self.cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                temp_path = Path(handle.name)

            os.replace(temp_path, self.cache_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
