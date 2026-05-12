import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.alerts.base import NullAlertSink
from tidal.auctionscan import AuctionScanEnrichmentResult
from tidal.config import MonitoredFeeBurner
from tidal.constants import ADDITIONAL_DISCOVERY_VAULTS, CORE_REWARD_TOKENS
from tidal.persistence import models
from tidal.persistence.repositories import (
    AuctionEnabledTokenRepository,
    AuctionEnabledTokenScanRepository,
    BalanceRepository,
    FeeBurnerRepository,
    FeeBurnerTokenBalanceRepository,
    FeeBurnerTokenRepository,
    ScanItemErrorRepository,
    ScanRunRepository,
    StrategyRepository,
    StrategyTokenRepository,
    TokenRepository,
    VaultRepository,
)
from tidal.scanner.service import ScannerService
from tidal.scanner.auction_mapper import AuctionMappingRefreshResult, FeeBurnerAuctionRefreshResult
from tidal.scanner.auction_token_enabler import (
    AuctionTokenEnablementPassResult,
    AuctionTokenEnablementStats,
)
from tidal.scanner.token_metadata import TokenMetadataService
from tidal.types import BalancePair, DiscoveredStrategy


class FakeWeb3Client:
    async def get_block_number(self) -> int:
        return 20202020


class FakeAuctionStateReader:
    def __init__(self, values_by_auction=None, *, error: Exception | None = None) -> None:
        self.values_by_auction = values_by_auction or {}
        self.error = error

    async def read_address_array_noargs_many(self, auction_addresses: list[str], method_name: str):
        assert method_name == "getAllEnabledAuctions"
        if self.error is not None:
            raise self.error
        return {auction_address: self.values_by_auction.get(auction_address) for auction_address in auction_addresses}


class FakeDiscoveryService:
    async def discover(self) -> tuple[list[DiscoveredStrategy], int, dict[str, int]]:
        return (
            [
                DiscoveredStrategy(
                    strategy_address="0x1111111111111111111111111111111111111111",
                    vault_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
                DiscoveredStrategy(
                    strategy_address="0x2222222222222222222222222222222222222222",
                    vault_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
            ],
            1,
            {
                "batch_count": 1,
                "subcalls_total": 8,
                "subcalls_failed": 0,
                "fallback_direct_calls_total": 0,
                "overflow_vaults_count": 0,
            },
        )


class FakeRewardTokenResolver:
    async def resolve(self, strategy_address: str) -> set[str]:
        del strategy_address
        return set(CORE_REWARD_TOKENS).union({"0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"})

    async def resolve_many(self, strategy_addresses: list[str]) -> tuple[dict[str, set[str]], dict[str, int]]:
        resolved = {address: await self.resolve(address) for address in strategy_addresses}
        return (
            resolved,
            {
                "batch_count": 1,
                "subcalls_total": len(strategy_addresses),
                "subcalls_failed": 0,
                "fallback_direct_calls_total": 0,
            },
        )


class FakeERC20Reader:
    def __init__(self) -> None:
        self.decimals_calls = 0

    async def read_decimals(self, token_address: str) -> int:
        del token_address
        self.decimals_calls += 1
        return 6

    async def read_symbol(self, token_address: str) -> str:
        del token_address
        return "USDC"

    async def read_name(self, token_address: str) -> str:
        del token_address
        return "USD Coin"


class FakeBalanceReader:
    async def read(self, strategy_address: str, token_address: str) -> int:
        del strategy_address
        del token_address
        return 1_000_000

    async def read_many(self, pairs: list[BalancePair]) -> tuple[dict[BalancePair, int | None], dict[str, int]]:
        return (
            {pair: 1_000_000 for pair in pairs},
            {
                "batch_count": 1,
                "subcalls_total": len(pairs),
                "subcalls_failed": 0,
                "fallback_direct_calls_total": 0,
            },
        )


class FakeNameReader:
    def __init__(self) -> None:
        self.vault_calls = 0
        self.vault_symbol_calls = 0
        self.strategy_calls = 0

    async def read_vault_name(self, vault_address: str) -> str | None:
        del vault_address
        self.vault_calls += 1
        return "Test Vault"

    async def read_vault_symbol(self, vault_address: str) -> str | None:
        del vault_address
        self.vault_symbol_calls += 1
        return "yvTEST"

    async def read_vault_deposit_limit(self, vault_address: str) -> str | None:
        del vault_address
        return "1"

    async def read_strategy_name(self, strategy_address: str) -> str | None:
        self.strategy_calls += 1
        return f"Strategy {strategy_address[-4:]}"


class FakeTokenPriceRefreshService:
    def __init__(self) -> None:
        self.calls = 0
        self.last_tokens = []

    async def refresh_many(self, *, run_id: str, tokens):
        del run_id
        self.calls += 1
        self.last_tokens = list(tokens)
        return (
            {
                "tokens_seen": len(tokens),
                "tokens_succeeded": len(tokens),
                "tokens_not_found": 0,
                "tokens_failed": 0,
            },
            [],
        )


class FakeFeeBurnerTokenResolver:
    def __init__(self, tokens_by_burner=None):
        self.tokens_by_burner = tokens_by_burner or {}

    async def resolve_many(self, fee_burners):
        return (
            {fee_burner.address.lower(): set(self.tokens_by_burner.get(fee_burner.address.lower(), set())) for fee_burner in fee_burners},
            [],
        )


class FakeAuctionScanService:
    def __init__(
        self,
        *,
        result: AuctionScanEnrichmentResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or AuctionScanEnrichmentResult()
        self.error = error
        self.calls: list[int] = []

    async def enrich_pending_kicks(self, *, limit: int) -> AuctionScanEnrichmentResult:
        self.calls.append(limit)
        if self.error is not None:
            raise self.error
        return self.result


class FakeAuctionTokenEnabler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def enable_missing_tokens(
        self,
        *,
        run_id: str,
        candidates,
        enabled_tokens_by_auction,
    ):  # noqa: ANN001
        self.calls.append(
            {
                "run_id": run_id,
                "candidates": list(candidates),
                "enabled_tokens_by_auction": enabled_tokens_by_auction,
            }
        )
        return AuctionTokenEnablementPassResult(
            stats=AuctionTokenEnablementStats(
                auctions_seen=len({candidate.source.auction_address for candidate in candidates}),
                candidates_seen=len(candidates),
                eligible_tokens=len(candidates),
                tokens_confirmed=len(candidates),
            ),
            errors=[],
        )


class FakeStrategyAuctionMapper:
    def __init__(self, *, fail_refresh: bool = False, strategy_to_want: dict[str, str | None] | None = None) -> None:
        self.fail_refresh = fail_refresh
        self.strategy_to_want = strategy_to_want or {}
        self.refresh_calls = 0
        self.cached_mapping = {
            "0x1111111111111111111111111111111111111111": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "0x2222222222222222222222222222222222222222": None,
        }
        self.cached_versions = {
            "0x1111111111111111111111111111111111111111": "1.0.0",
            "0x2222222222222222222222222222222222222222": None,
        }

    async def refresh_for_strategies(self, strategy_addresses: list[str]) -> AuctionMappingRefreshResult:
        self.refresh_calls += 1
        if self.fail_refresh:
            raise RuntimeError("auction mapping rpc failed")

        strategy_set = sorted(set(strategy_addresses))
        mapped_count = sum(1 for strategy in strategy_set if self.cached_mapping.get(strategy))
        return AuctionMappingRefreshResult(
            strategy_to_auction={strategy: self.cached_mapping.get(strategy) for strategy in strategy_set},
            strategy_to_want={strategy: self.strategy_to_want.get(strategy) for strategy in strategy_set},
            strategy_to_auction_version={strategy: self.cached_versions.get(strategy) for strategy in strategy_set},
            auction_count=4,
            valid_auction_count=2,
            receiver_filtered_count=0,
            mapped_count=mapped_count,
            unmapped_count=len(strategy_set) - mapped_count,
            source="fresh",
        )

    async def refresh_for_fee_burners(self, fee_burner_to_want: dict[str, str]) -> FeeBurnerAuctionRefreshResult:
        fee_burner_to_auction = {
            address: f"0x{index:040x}"
            for index, address in enumerate(sorted(fee_burner_to_want), start=1)
        }
        fee_burner_to_auction_version = {address: "1.0.3cc" for address in fee_burner_to_want}
        return FeeBurnerAuctionRefreshResult(
            fee_burner_to_auction=fee_burner_to_auction,
            fee_burner_to_want=fee_burner_to_want,
            fee_burner_to_auction_version=fee_burner_to_auction_version,
            fee_burner_to_error={},
            auction_count=4,
            valid_auction_count=2,
            receiver_filtered_count=0,
            mapped_count=len(fee_burner_to_want),
            unmapped_count=0,
            source="fresh",
        )


@pytest.mark.asyncio
async def test_scanner_persists_lowercase_and_zero_balances() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )
        fake_name_reader = FakeNameReader()
        fake_token_price_refresh_service = FakeTokenPriceRefreshService()
        fake_strategy_auction_mapper = FakeStrategyAuctionMapper(
            strategy_to_want={
                "0x1111111111111111111111111111111111111111": "0x4000000000000000000000000000000000000004",
            },
        )

        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=fake_strategy_auction_mapper,
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=fake_token_price_refresh_service,
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=fake_name_reader,
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]}
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=None,
            auctionscan_enrichment_batch_size=0,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()
        result_second = await scanner.scan_once()

        assert result.status == "SUCCESS"
        assert result.pairs_seen == 6
        assert result_second.status == "SUCCESS"

        tokens_rows = session.execute(select(models.tokens)).mappings().all()
        assert len(tokens_rows) == 4
        token_addresses = {row["address"] for row in tokens_rows}
        assert "0x4000000000000000000000000000000000000004" in token_addresses
        assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in token_addresses

        balance_rows = session.execute(select(models.strategy_token_balances_latest)).mappings().all()
        assert len(balance_rows) == 6
        assert all(row["normalized_balance"] == "1" for row in balance_rows)
        assert all(row["strategy_address"] == row["strategy_address"].lower() for row in balance_rows)
        assert all(row["token_address"] == row["token_address"].lower() for row in balance_rows)

        # Shared token metadata should only be fetched once per unique token.
        assert fake_erc20.decimals_calls == 4
        # Price refresh runs once per scan and dedupes token addresses.
        assert fake_token_price_refresh_service.calls == 2
        assert len(fake_token_price_refresh_service.last_tokens) == 3
        # Auction mapping runs once per scan.
        assert fake_strategy_auction_mapper.refresh_calls == 2
        # Names are fetched once per vault then reused from DB cache on subsequent scans.
        assert fake_name_reader.vault_calls == 2
        assert fake_name_reader.vault_symbol_calls == 2
        assert fake_name_reader.strategy_calls == 2

        vault_rows = session.execute(select(models.vaults)).mappings().all()
        assert len(vault_rows) == 2
        expected_vaults = {
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            next(iter(ADDITIONAL_DISCOVERY_VAULTS)),
        }
        assert {row["address"] for row in vault_rows} == expected_vaults
        assert all(row["name"] == "Test Vault" for row in vault_rows)
        assert all(row["symbol"] == "yvTEST" for row in vault_rows)

        strategy_rows = session.execute(select(models.strategies)).mappings().all()
        assert all(row["name"] is not None for row in strategy_rows)
        strategy_rows_by_address = {row["address"]: row for row in strategy_rows}
        assert strategy_rows_by_address["0x1111111111111111111111111111111111111111"]["auction_address"] == (
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        assert strategy_rows_by_address["0x1111111111111111111111111111111111111111"]["auction_version"] == "1.0.0"
        assert strategy_rows_by_address["0x2222222222222222222222222222222222222222"]["auction_address"] is None
        assert strategy_rows_by_address["0x2222222222222222222222222222222222222222"]["auction_version"] is None

        enabled_token_rows = session.execute(select(models.auction_enabled_tokens_latest)).mappings().all()
        assert len(enabled_token_rows) == 1
        assert enabled_token_rows[0]["auction_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert enabled_token_rows[0]["token_address"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        assert enabled_token_rows[0]["active"] == 1
        enabled_scan_rows = session.execute(select(models.auction_enabled_token_scans)).mappings().all()
        assert len(enabled_scan_rows) == 1
        assert enabled_scan_rows[0]["auction_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert enabled_scan_rows[0]["status"] == "SUCCESS"
        assert enabled_scan_rows[0]["block_number"] == 20202020


@pytest.mark.asyncio
async def test_scanner_auto_enable_runs_after_balance_reads_with_positive_balance_candidates() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )
        fake_auto_enabler = FakeAuctionTokenEnabler()
        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=FakeStrategyAuctionMapper(),
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=fake_auto_enabler,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]}
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=None,
            auctionscan_enrichment_batch_size=0,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()

        assert result.status == "SUCCESS"
        assert len(fake_auto_enabler.calls) == 1
        call = fake_auto_enabler.calls[0]
        candidates = call["candidates"]
        assert len(candidates) == 3
        assert {candidate.source.source_address for candidate in candidates} == {
            "0x1111111111111111111111111111111111111111"
        }
        assert all(candidate.balance_raw == 1_000_000 for candidate in candidates)
        assert call["enabled_tokens_by_auction"] == {
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
                "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
            }
        }


@pytest.mark.asyncio
async def test_scanner_persists_fee_burner_rows_and_balances() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )
        fee_burner = MonitoredFeeBurner(
            address="0xb911fcce8d5afcec73e072653107260bb23c1ee8",
            want_address="0xf939e0a03fb07f59a73314e73794be0e57ac1b4e",
            label="Yearn Fee Burner",
        )

        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=FakeStrategyAuctionMapper(),
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[fee_burner],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(
                tokens_by_burner={fee_burner.address.lower(): {"0xcccccccccccccccccccccccccccccccccccccccc"}}
            ),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={
                    "0x0000000000000000000000000000000000000001": ["0xcccccccccccccccccccccccccccccccccccccccc"]
                }
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=None,
            auctionscan_enrichment_batch_size=0,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()

        assert result.status == "SUCCESS"

        fee_burner_rows = session.execute(select(models.fee_burners)).mappings().all()
        assert len(fee_burner_rows) == 1
        assert fee_burner_rows[0]["address"] == fee_burner.address.lower()
        assert fee_burner_rows[0]["name"] == "Yearn Fee Burner"
        assert fee_burner_rows[0]["want_address"] == fee_burner.want_address.lower()
        assert fee_burner_rows[0]["auction_address"] is not None
        assert fee_burner_rows[0]["auction_version"] == "1.0.3cc"

        fee_burner_token_rows = session.execute(select(models.fee_burner_tokens)).mappings().all()
        assert len(fee_burner_token_rows) == 1
        assert fee_burner_token_rows[0]["source"] == "trade_handler_approval"

        fee_burner_balance_rows = session.execute(select(models.fee_burner_token_balances_latest)).mappings().all()
        assert len(fee_burner_balance_rows) == 1
        assert fee_burner_balance_rows[0]["fee_burner_address"] == fee_burner.address.lower()
        assert fee_burner_balance_rows[0]["normalized_balance"] == "1"

        enabled_token_rows = session.execute(select(models.auction_enabled_tokens_latest)).mappings().all()
        assert any(
            row["auction_address"] == "0x0000000000000000000000000000000000000001"
            and row["token_address"] == "0xcccccccccccccccccccccccccccccccccccccccc"
            and row["active"] == 1
            for row in enabled_token_rows
        )

        enabled_scan_rows = session.execute(select(models.auction_enabled_token_scans)).mappings().all()
        assert any(
            row["auction_address"] == "0x0000000000000000000000000000000000000001"
            and row["status"] == "SUCCESS"
            for row in enabled_scan_rows
        )


@pytest.mark.asyncio
async def test_scanner_uses_cached_auction_mapping_when_refresh_fails() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )

        healthy_mapper = FakeStrategyAuctionMapper()
        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=healthy_mapper,
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]}
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=None,
            auctionscan_enrichment_batch_size=0,
            alert_sink=NullAlertSink(),
        )

        initial_result = await scanner.scan_once()
        assert initial_result.status == "SUCCESS"

        failing_scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=FakeStrategyAuctionMapper(fail_refresh=True),
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(error=RuntimeError("enabled-token rpc failed")),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=None,
            auctionscan_enrichment_batch_size=0,
            alert_sink=NullAlertSink(),
        )

        result = await failing_scanner.scan_once()

        assert result.status == "SUCCESS"
        error_rows = session.execute(select(models.scan_item_errors)).mappings().all()
        assert any(
            row["stage"] == "AUCTION_MAPPING"
            and row["error_code"] == "strategy_auction_mapping_failed"
            for row in error_rows
        )
        strategy_rows = session.execute(select(models.strategies)).mappings().all()
        strategy_rows_by_address = {row["address"]: row for row in strategy_rows}
        assert strategy_rows_by_address["0x1111111111111111111111111111111111111111"]["auction_address"] == (
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        assert strategy_rows_by_address["0x1111111111111111111111111111111111111111"]["auction_version"] == "1.0.0"
        assert strategy_rows_by_address["0x1111111111111111111111111111111111111111"]["auction_error_message"] == (
            "auction mapping rpc failed"
        )

        enabled_token_rows = session.execute(select(models.auction_enabled_tokens_latest)).mappings().all()
        assert len(enabled_token_rows) == 1
        assert enabled_token_rows[0]["auction_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert enabled_token_rows[0]["token_address"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        assert enabled_token_rows[0]["active"] == 1

        enabled_scan_rows = session.execute(select(models.auction_enabled_token_scans)).mappings().all()
        assert len(enabled_scan_rows) == 1
        assert enabled_scan_rows[0]["auction_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert enabled_scan_rows[0]["status"] == "FAILED"
        assert enabled_scan_rows[0]["error_message"] == "enabled-token rpc failed"


@pytest.mark.asyncio
async def test_scanner_runs_auctionscan_enrichment_at_end() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )
        fake_auctionscan = FakeAuctionScanService(
            result=AuctionScanEnrichmentResult(
                candidates_seen=3,
                kicks_checked=3,
                kicks_resolved=2,
                kicks_unresolved=1,
                kicks_failed=0,
            )
        )

        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=FakeStrategyAuctionMapper(),
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]}
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=fake_auctionscan,
            auctionscan_enrichment_batch_size=7,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()

        assert result.status == "SUCCESS"
        assert fake_auctionscan.calls == [7]
        error_rows = session.execute(select(models.scan_item_errors)).mappings().all()
        assert not any(row["stage"] == "AUCTIONSCAN_ENRICHMENT" for row in error_rows)


@pytest.mark.asyncio
async def test_scanner_keeps_scan_success_when_auctionscan_enrichment_fails() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        fee_burner_repo = FeeBurnerRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        fee_burner_token_repo = FeeBurnerTokenRepository(session)
        balance_repo = BalanceRepository(session)
        fee_burner_balance_repo = FeeBurnerTokenBalanceRepository(session)
        auction_enabled_token_repo = AuctionEnabledTokenRepository(session)
        auction_enabled_token_scan_repo = AuctionEnabledTokenScanRepository(session)
        scan_run_repo = ScanRunRepository(session)
        scan_item_error_repo = ScanItemErrorRepository(session)

        fake_erc20 = FakeERC20Reader()
        token_metadata_service = TokenMetadataService(
            chain_id=1,
            token_repository=token_repo,
            erc20_reader=fake_erc20,
        )

        scanner = ScannerService(
            session=session,
            chain_id=1,
            concurrency=5,
            multicall_enabled=True,
            web3_client=FakeWeb3Client(),
            strategy_auction_mapper=FakeStrategyAuctionMapper(),
            strategy_discovery_service=FakeDiscoveryService(),
            reward_token_resolver=FakeRewardTokenResolver(),
            token_metadata_service=token_metadata_service,
            token_price_refresh_service=FakeTokenPriceRefreshService(),
            balance_reader=FakeBalanceReader(),
            auction_settler=None,
            auction_token_enabler=None,
            monitored_fee_burners=[],
            fee_burner_token_resolver=FakeFeeBurnerTokenResolver(),
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            fee_burner_repository=fee_burner_repo,
            strategy_token_repository=strategy_token_repo,
            fee_burner_token_repository=fee_burner_token_repo,
            balance_repository=balance_repo,
            fee_burner_balance_repository=fee_burner_balance_repo,
            auction_state_reader=FakeAuctionStateReader(
                values_by_auction={"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]}
            ),
            auction_enabled_token_repository=auction_enabled_token_repo,
            auction_enabled_token_scan_repository=auction_enabled_token_scan_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            auctionscan_service=FakeAuctionScanService(error=RuntimeError("auctionscan unavailable")),
            auctionscan_enrichment_batch_size=5,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()

        assert result.status == "SUCCESS"
        error_rows = session.execute(select(models.scan_item_errors)).mappings().all()
        assert any(
            row["stage"] == "AUCTIONSCAN_ENRICHMENT"
            and row["error_code"] == "auctionscan_enrichment_failed"
            and row["error_message"] == "auctionscan unavailable"
            for row in error_rows
        )
