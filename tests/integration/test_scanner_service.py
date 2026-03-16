import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from factory_dashboard.alerts.base import NullAlertSink
from factory_dashboard.constants import ADDITIONAL_DISCOVERY_VAULTS, CORE_REWARD_TOKENS
from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import (
    BalanceRepository,
    ScanItemErrorRepository,
    ScanRunRepository,
    StrategyRepository,
    StrategyTokenRepository,
    TokenRepository,
    VaultRepository,
)
from factory_dashboard.scanner.service import ScannerService
from factory_dashboard.scanner.auction_mapper import AuctionMappingRefreshResult
from factory_dashboard.scanner.token_metadata import TokenMetadataService
from factory_dashboard.types import BalancePair, DiscoveredStrategy


class FakeWeb3Client:
    async def get_block_number(self) -> int:
        return 20202020


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


class FakeStrategyAuctionMapper:
    def __init__(self, *, fail_refresh: bool = False) -> None:
        self.fail_refresh = fail_refresh
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
            strategy_to_want={strategy: None for strategy in strategy_set},
            strategy_to_auction_version={strategy: self.cached_versions.get(strategy) for strategy in strategy_set},
            auction_count=4,
            valid_auction_count=2,
            receiver_filtered_count=0,
            mapped_count=mapped_count,
            unmapped_count=len(strategy_set) - mapped_count,
            source="fresh",
        )


@pytest.mark.asyncio
async def test_scanner_persists_lowercase_and_zero_balances() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        balance_repo = BalanceRepository(session)
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
        fake_strategy_auction_mapper = FakeStrategyAuctionMapper()

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
            name_reader=fake_name_reader,
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            strategy_token_repository=strategy_token_repo,
            balance_repository=balance_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
            alert_sink=NullAlertSink(),
        )

        result = await scanner.scan_once()
        result_second = await scanner.scan_once()

        assert result.status == "SUCCESS"
        assert result.pairs_seen == 6
        assert result_second.status == "SUCCESS"

        tokens_rows = session.execute(select(models.tokens)).mappings().all()
        assert len(tokens_rows) == 3
        token_addresses = {row["address"] for row in tokens_rows}
        assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in token_addresses

        balance_rows = session.execute(select(models.strategy_token_balances_latest)).mappings().all()
        assert len(balance_rows) == 6
        assert all(row["normalized_balance"] == "1" for row in balance_rows)
        assert all(row["strategy_address"] == row["strategy_address"].lower() for row in balance_rows)
        assert all(row["token_address"] == row["token_address"].lower() for row in balance_rows)

        # Shared token metadata should only be fetched once per unique token.
        assert fake_erc20.decimals_calls == 3
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


@pytest.mark.asyncio
async def test_scanner_uses_cached_auction_mapping_when_refresh_fails() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        vault_repo = VaultRepository(session)
        strategy_repo = StrategyRepository(session)
        token_repo = TokenRepository(session)
        strategy_token_repo = StrategyTokenRepository(session)
        balance_repo = BalanceRepository(session)
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
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            strategy_token_repository=strategy_token_repo,
            balance_repository=balance_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
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
            name_reader=FakeNameReader(),
            vault_repository=vault_repo,
            strategy_repository=strategy_repo,
            strategy_token_repository=strategy_token_repo,
            balance_repository=balance_repo,
            scan_run_repository=scan_run_repo,
            scan_item_error_repository=scan_item_error_repo,
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
