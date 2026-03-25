"""Strategy-to-auction mapping refresh."""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode as abi_decode
from hexbytes import HexBytes

from factory_dashboard.chain.contracts.abis import AUCTION_ABI, AUCTION_FACTORY_ABI, STRATEGY_ABI
from factory_dashboard.chain.contracts.multicall import MulticallClient, MulticallRequest
from factory_dashboard.constants import ZERO_ADDRESS
from factory_dashboard.normalizers import normalize_address

_AUCTION_METADATA_FIELDS = ("governance", "want", "receiver", "version")
_AUCTION_ADDRESS_FIELDS = frozenset({"governance", "want", "receiver"})


@dataclass(slots=True)
class AuctionMetadata:
    governance: str | None = None
    want: str | None = None
    receiver: str | None = None
    version: str | None = None


@dataclass(slots=True)
class AuctionMappingRefreshResult:
    strategy_to_auction: dict[str, str | None]
    strategy_to_want: dict[str, str | None]
    strategy_to_auction_version: dict[str, str | None]
    auction_count: int
    valid_auction_count: int
    receiver_filtered_count: int
    mapped_count: int
    unmapped_count: int
    source: str


@dataclass(slots=True)
class FeeBurnerAuctionRefreshResult:
    fee_burner_to_auction: dict[str, str | None]
    fee_burner_to_want: dict[str, str | None]
    fee_burner_to_auction_version: dict[str, str | None]
    fee_burner_to_error: dict[str, str]
    auction_count: int
    valid_auction_count: int
    receiver_filtered_count: int
    mapped_count: int
    unmapped_count: int
    source: str


class StrategyAuctionMapper:
    """Builds source->auction mappings from the auction factory snapshot."""

    def __init__(
        self,
        *,
        web3_client,
        chain_id: int,
        auction_factory_address: str,
        required_governance_address: str,
        multicall_client: MulticallClient | None = None,
        multicall_enabled: bool = True,
        multicall_auction_batch_calls: int = 500,
    ) -> None:
        self.web3_client = web3_client
        self.chain_id = chain_id
        self.auction_factory_address = normalize_address(auction_factory_address)
        self.required_governance_address = normalize_address(required_governance_address)
        self.multicall_client = multicall_client
        self.multicall_enabled = multicall_enabled
        self.multicall_auction_batch_calls = multicall_auction_batch_calls

    async def refresh_for_strategies(self, strategy_addresses: list[str]) -> AuctionMappingRefreshResult:
        normalized_strategies = sorted({normalize_address(address) for address in strategy_addresses})

        auction_addresses, valid_auctions, receiver_filtered_count = await self._load_valid_auctions()

        # Build lookup keyed by (want, receiver) — latest by factory order wins.
        want_receiver_to_auction: dict[tuple[str, str], tuple[str, AuctionMetadata]] = {}
        for auction_address, meta in valid_auctions:
            want_receiver_to_auction[(meta.want, meta.receiver)] = (auction_address, meta)

        strategy_to_auction: dict[str, str | None] = {}
        strategy_to_auction_version: dict[str, str | None] = {}
        strategy_wants = await self._read_strategy_wants_many(normalized_strategies)

        for strategy_address in normalized_strategies:
            strategy_want = strategy_wants.get(strategy_address)
            match = None
            if strategy_want is not None:
                match = want_receiver_to_auction.get((strategy_want, strategy_address))
            if match is not None:
                strategy_to_auction[strategy_address] = match[0]
                strategy_to_auction_version[strategy_address] = match[1].version
            else:
                strategy_to_auction[strategy_address] = None
                strategy_to_auction_version[strategy_address] = None

        mapped_count = sum(1 for auction_address in strategy_to_auction.values() if auction_address)
        unmapped_count = len(strategy_to_auction) - mapped_count

        return AuctionMappingRefreshResult(
            strategy_to_auction=strategy_to_auction,
            strategy_to_want=strategy_wants,
            strategy_to_auction_version=strategy_to_auction_version,
            auction_count=len(auction_addresses),
            valid_auction_count=len(valid_auctions),
            receiver_filtered_count=receiver_filtered_count,
            mapped_count=mapped_count,
            unmapped_count=unmapped_count,
            source="fresh",
        )

    async def refresh_for_fee_burners(self, fee_burner_to_want: dict[str, str]) -> FeeBurnerAuctionRefreshResult:
        normalized_fee_burners = {
            normalize_address(address): normalize_address(want_address)
            for address, want_address in fee_burner_to_want.items()
        }

        auction_addresses, valid_auctions, receiver_filtered_count = await self._load_valid_auctions()
        matches_by_key: dict[tuple[str, str], list[tuple[str, AuctionMetadata]]] = {}
        for auction_address, meta in valid_auctions:
            key = (meta.want, meta.receiver)
            matches_by_key.setdefault(key, []).append((auction_address, meta))

        fee_burner_to_auction: dict[str, str | None] = {}
        fee_burner_to_auction_version: dict[str, str | None] = {}
        fee_burner_to_error: dict[str, str] = {}

        for fee_burner_address, want_address in normalized_fee_burners.items():
            matches = matches_by_key.get((want_address, fee_burner_address), [])
            if len(matches) == 1:
                fee_burner_to_auction[fee_burner_address] = matches[0][0]
                fee_burner_to_auction_version[fee_burner_address] = matches[0][1].version
                continue
            fee_burner_to_auction[fee_burner_address] = None
            fee_burner_to_auction_version[fee_burner_address] = None
            if not matches:
                fee_burner_to_error[fee_burner_address] = "no matching auction found for configured want/receiver"
            else:
                fee_burner_to_error[fee_burner_address] = "multiple matching auctions found for configured want/receiver"

        mapped_count = sum(1 for auction_address in fee_burner_to_auction.values() if auction_address)
        unmapped_count = len(normalized_fee_burners) - mapped_count

        return FeeBurnerAuctionRefreshResult(
            fee_burner_to_auction=fee_burner_to_auction,
            fee_burner_to_want=normalized_fee_burners,
            fee_burner_to_auction_version=fee_burner_to_auction_version,
            fee_burner_to_error=fee_burner_to_error,
            auction_count=len(auction_addresses),
            valid_auction_count=len(valid_auctions),
            receiver_filtered_count=receiver_filtered_count,
            mapped_count=mapped_count,
            unmapped_count=unmapped_count,
            source="fresh",
        )

    async def _load_valid_auctions(self) -> tuple[list[str], list[tuple[str, AuctionMetadata]], int]:
        auction_addresses = await self._read_auction_addresses()
        auction_metadata = await self._read_auction_metadata_many(auction_addresses)

        valid_auctions: list[tuple[str, AuctionMetadata]] = []
        receiver_filtered_count = 0

        for auction_address in auction_addresses:
            meta = auction_metadata.get(auction_address)
            if meta is None:
                continue
            if meta.governance != self.required_governance_address:
                continue
            if meta.want is None or meta.want == ZERO_ADDRESS:
                continue
            if meta.receiver is None or meta.receiver == ZERO_ADDRESS:
                receiver_filtered_count += 1
                continue
            valid_auctions.append((auction_address, meta))

        return auction_addresses, valid_auctions, receiver_filtered_count

    async def _read_auction_addresses(self) -> list[str]:
        factory = self.web3_client.contract(self.auction_factory_address, AUCTION_FACTORY_ABI)
        result = await self.web3_client.call(factory.functions.getAllAuctions())
        return [normalize_address(address) for address in result]

    async def _read_auction_metadata_many(self, auction_addresses: list[str]) -> dict[str, AuctionMetadata]:
        output: dict[str, AuctionMetadata] = {
            auction_address: AuctionMetadata()
            for auction_address in auction_addresses
        }
        if not auction_addresses:
            return output

        if not self.multicall_enabled or self.multicall_client is None:
            for auction_address in auction_addresses:
                auction_contract = self.web3_client.contract(auction_address, AUCTION_ABI)
                meta = output[auction_address]
                try:
                    meta.governance = normalize_address(
                        await self.web3_client.call(auction_contract.functions.governance())
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    meta.want = normalize_address(
                        await self.web3_client.call(auction_contract.functions.want())
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    meta.receiver = normalize_address(
                        await self.web3_client.call(auction_contract.functions.receiver())
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    meta.version = await self.web3_client.call(
                        auction_contract.functions.version()
                    )
                except Exception:  # noqa: BLE001
                    pass
            return output

        requests: list[MulticallRequest] = []
        for auction_address in auction_addresses:
            auction_contract = self.web3_client.contract(auction_address, AUCTION_ABI)
            for field_name in _AUCTION_METADATA_FIELDS:
                fn = getattr(auction_contract.functions, field_name)()
                requests.append(
                    MulticallRequest(
                        target=auction_address,
                        call_data=bytes(HexBytes(fn._encode_transaction_data())),
                        logical_key=(auction_address, field_name),
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
                if field in _AUCTION_ADDRESS_FIELDS:
                    decoded = normalize_address(abi_decode(["address"], result.return_data)[0])
                else:
                    decoded = abi_decode(["string"], result.return_data)[0]
            except Exception:  # noqa: BLE001
                continue
            setattr(output[auction_address], field, decoded)

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
