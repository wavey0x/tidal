"""Service wiring utilities."""

from __future__ import annotations

from tidal.alerts.base import NullAlertSink
from tidal.alerts.telegram import TelegramAlertSink
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.chain.contracts.multicall import MulticallClient
from tidal.chain.contracts.yearn import StrategyRewardsReader, YearnCurveFactoryReader, YearnNameReader
from tidal.chain.web3_client import Web3Client
from tidal.config import Settings
from tidal.constants import (
    YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS,
    YEARN_CURVE_FACTORY_ADDRESS,
)
from tidal.persistence.repositories import (
    BalanceRepository,
    ScanItemErrorRepository,
    ScanRunRepository,
    StrategyRepository,
    StrategyTokenRepository,
    TokenRepository,
    VaultRepository,
)
from tidal.pricing.token_price_agg import TokenPriceAggProvider
from tidal.pricing.token_logo import TokenLogoValidator
from tidal.pricing.service import TokenPriceRefreshService
from tidal.scanner.balance_reader import BalanceReader
from tidal.scanner.discovery import StrategyDiscoveryService
from tidal.scanner.auction_mapper import StrategyAuctionMapper
from tidal.scanner.reward_token_resolver import RewardTokenResolver
from tidal.scanner.service import ScannerService
from tidal.scanner.token_metadata import TokenMetadataService


def build_scanner_service(settings: Settings, session) -> ScannerService:
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
    strategy_rewards_reader = StrategyRewardsReader(
        web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_rewards_batch_calls=settings.multicall_rewards_batch_calls,
        multicall_rewards_index_max=settings.multicall_rewards_index_max,
    )
    erc20_reader = ERC20Reader(
        web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_balance_batch_calls=settings.multicall_balance_batch_calls,
    )
    yearn_name_reader = YearnNameReader(web3_client)

    vault_repository = VaultRepository(session)
    strategy_repository = StrategyRepository(session)
    token_repository = TokenRepository(session)
    strategy_token_repository = StrategyTokenRepository(session)
    balance_repository = BalanceRepository(session)
    scan_run_repository = ScanRunRepository(session)
    scan_item_error_repository = ScanItemErrorRepository(session)

    token_metadata_service = TokenMetadataService(
        settings.chain_id,
        token_repository,
        erc20_reader,
    )
    token_price_refresh_service = TokenPriceRefreshService(
        chain_id=settings.chain_id,
        enabled=settings.price_refresh_enabled,
        concurrency=settings.price_concurrency,
        price_provider=TokenPriceAggProvider(
            chain_id=settings.chain_id,
            base_url=settings.token_price_agg_base_url,
            api_key=settings.token_price_agg_key,
            timeout_seconds=settings.price_timeout_seconds,
            retry_attempts=settings.price_retry_attempts,
        ),
        logo_validator=TokenLogoValidator(
            timeout_seconds=settings.price_timeout_seconds,
            retry_attempts=settings.price_retry_attempts,
        ),
        token_repository=token_repository,
    )

    if settings.telegram_alerts_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
        alert_sink = TelegramAlertSink(settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        alert_sink = NullAlertSink()

    return ScannerService(
        session=session,
        chain_id=settings.chain_id,
        concurrency=settings.scan_concurrency,
        multicall_enabled=settings.multicall_enabled,
        web3_client=web3_client,
        strategy_auction_mapper=StrategyAuctionMapper(
            web3_client=web3_client,
            chain_id=settings.chain_id,
            auction_factory_address=settings.auction_factory_address,
            required_governance_address=YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS,
            multicall_client=multicall_client,
            multicall_enabled=settings.multicall_enabled,
            multicall_auction_batch_calls=settings.multicall_auction_batch_calls,
        ),
        strategy_discovery_service=StrategyDiscoveryService(
            yearn_reader,
            concurrency=settings.scan_concurrency,
        ),
        reward_token_resolver=RewardTokenResolver(strategy_rewards_reader),
        token_metadata_service=token_metadata_service,
        token_price_refresh_service=token_price_refresh_service,
        balance_reader=BalanceReader(erc20_reader),
        name_reader=yearn_name_reader,
        vault_repository=vault_repository,
        strategy_repository=strategy_repository,
        strategy_token_repository=strategy_token_repository,
        balance_repository=balance_repository,
        scan_run_repository=scan_run_repository,
        scan_item_error_repository=scan_item_error_repository,
        alert_sink=alert_sink,
    )


def build_web3_client(settings: Settings) -> Web3Client:
    return Web3Client(
        settings.rpc_url,
        timeout_seconds=settings.rpc_timeout_seconds,
        retry_attempts=settings.rpc_retry_attempts,
    )
