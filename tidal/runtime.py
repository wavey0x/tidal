"""Service wiring utilities."""

from __future__ import annotations

from tidal.alerts.base import NullAlertSink
from tidal.alerts.telegram import TelegramAlertSink
from tidal.chain.contracts.fee_burner import FeeBurnerReader
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
    AuctionEnabledTokenRepository,
    AuctionEnabledTokenScanRepository,
    BalanceRepository,
    FeeBurnerRepository,
    FeeBurnerTokenBalanceRepository,
    FeeBurnerTokenRepository,
    KickTxRepository,
    ScanItemErrorRepository,
    ScanRunRepository,
    StrategyRepository,
    StrategyTokenRepository,
    TokenRepository,
    VaultRepository,
)
from tidal.pricing.token_price_agg import TokenPriceAggProvider
from tidal.pricing.service import TokenPriceRefreshService
from tidal.scanner.balance_reader import BalanceReader
from tidal.scanner.discovery import StrategyDiscoveryService
from tidal.scanner.auction_mapper import StrategyAuctionMapper
from tidal.scanner.auction_state import AuctionStateReader
from tidal.scanner.auction_settler import AuctionSettlementService
from tidal.scanner.fee_burner import FeeBurnerTokenResolver
from tidal.scanner.reward_token_resolver import RewardTokenResolver
from tidal.scanner.service import ScannerService
from tidal.scanner.token_metadata import TokenMetadataService
from tidal.transaction_service.signer import TransactionSigner
from tidal.transaction_service.pricing_policy import load_auction_pricing_policy, load_token_sizing_policy


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
    fee_burner_reader = FeeBurnerReader(web3_client)
    yearn_name_reader = YearnNameReader(web3_client)

    vault_repository = VaultRepository(session)
    strategy_repository = StrategyRepository(session)
    fee_burner_repository = FeeBurnerRepository(session)
    token_repository = TokenRepository(session)
    strategy_token_repository = StrategyTokenRepository(session)
    fee_burner_token_repository = FeeBurnerTokenRepository(session)
    balance_repository = BalanceRepository(session)
    fee_burner_balance_repository = FeeBurnerTokenBalanceRepository(session)
    auction_enabled_token_repository = AuctionEnabledTokenRepository(session)
    auction_enabled_token_scan_repository = AuctionEnabledTokenScanRepository(session)
    scan_run_repository = ScanRunRepository(session)
    scan_item_error_repository = ScanItemErrorRepository(session)
    kick_tx_repository = KickTxRepository(session)
    auction_state_reader = AuctionStateReader(
        web3_client=web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_auction_batch_calls=settings.multicall_auction_batch_calls,
    )

    token_metadata_service = TokenMetadataService(
        settings.chain_id,
        token_repository,
        erc20_reader,
    )
    token_price_refresh_service = TokenPriceRefreshService(
        chain_id=settings.chain_id,
        enabled=settings.price_refresh_enabled,
        concurrency=settings.price_concurrency,
        delay_seconds=settings.price_delay_seconds,
        price_provider=TokenPriceAggProvider(
            chain_id=settings.chain_id,
            base_url=settings.token_price_agg_base_url,
            api_key=settings.token_price_agg_key,
            timeout_seconds=settings.price_timeout_seconds,
            retry_attempts=settings.price_retry_attempts,
        ),
        token_repository=token_repository,
    )

    if settings.telegram_alerts_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
        alert_sink = TelegramAlertSink(settings.telegram_bot_token, settings.telegram_chat_id)
    else:
        alert_sink = NullAlertSink()

    auction_settler = None
    if settings.scan_auto_settle_enabled:
        signer = TransactionSigner(
            settings.txn_keystore_path,
            settings.txn_keystore_passphrase,
        )
        auction_settler = AuctionSettlementService(
            web3_client=web3_client,
            multicall_client=multicall_client,
            multicall_enabled=settings.multicall_enabled,
            multicall_auction_batch_calls=settings.multicall_auction_batch_calls,
            erc20_reader=erc20_reader,
            signer=signer,
            kick_tx_repository=kick_tx_repository,
            token_metadata_service=token_metadata_service,
            max_base_fee_gwei=settings.txn_max_base_fee_gwei,
            max_priority_fee_gwei=settings.txn_max_priority_fee_gwei,
            max_gas_limit=settings.txn_max_gas_limit,
            chain_id=settings.chain_id,
        )

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
        auction_settler=auction_settler,
        monitored_fee_burners=settings.monitored_fee_burners,
        fee_burner_token_resolver=FeeBurnerTokenResolver(
            fee_burner_reader,
            spender_address=YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS,
        ),
        name_reader=yearn_name_reader,
        vault_repository=vault_repository,
        strategy_repository=strategy_repository,
        fee_burner_repository=fee_burner_repository,
        strategy_token_repository=strategy_token_repository,
        fee_burner_token_repository=fee_burner_token_repository,
        balance_repository=balance_repository,
        fee_burner_balance_repository=fee_burner_balance_repository,
        auction_state_reader=auction_state_reader,
        auction_enabled_token_repository=auction_enabled_token_repository,
        auction_enabled_token_scan_repository=auction_enabled_token_scan_repository,
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


def build_txn_service(
    settings: Settings,
    session,
    *,
    confirm_fn=None,
    require_curve_quote: bool | None = None,
    skip_base_fee_check: bool = False,
    web3_client: Web3Client | None = None,
):
    from tidal.persistence.repositories import KickTxRepository, TxnRunRepository
    from tidal.transaction_service.kicker import AuctionKicker
    from tidal.transaction_service.service import TxnService
    from tidal.transaction_service.signer import TransactionSigner

    from tidal.pricing.token_price_agg import TokenPriceAggProvider as _TPA

    if web3_client is None:
        web3_client = build_web3_client(settings)
    txn_run_repository = TxnRunRepository(session)
    kick_tx_repository = KickTxRepository(session)

    signer = TransactionSigner(
        settings.txn_keystore_path,
        settings.txn_keystore_passphrase,
    )

    multicall_client = MulticallClient(
        web3_client,
        settings.multicall_address,
        enabled=settings.multicall_enabled,
    )
    erc20_reader = ERC20Reader(
        web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_balance_batch_calls=settings.multicall_balance_batch_calls,
    )
    auction_state_reader = AuctionStateReader(
        web3_client=web3_client,
        multicall_client=multicall_client,
        multicall_enabled=settings.multicall_enabled,
        multicall_auction_batch_calls=settings.multicall_auction_batch_calls,
    )
    pricing_policy = load_auction_pricing_policy()
    token_sizing_policy = load_token_sizing_policy()
    resolved_require_curve_quote = (
        settings.txn_require_curve_quote
        if require_curve_quote is None
        else require_curve_quote
    )

    price_provider = _TPA(
        chain_id=settings.chain_id,
        base_url=settings.token_price_agg_base_url,
        api_key=settings.token_price_agg_key,
        timeout_seconds=settings.price_timeout_seconds,
        retry_attempts=settings.price_retry_attempts,
    )

    kicker = AuctionKicker(
        web3_client=web3_client,
        signer=signer,
        kick_tx_repository=kick_tx_repository,
        price_provider=price_provider,
        auction_kicker_address=settings.auction_kicker_address,
        usd_threshold=settings.txn_usd_threshold,
        max_base_fee_gwei=settings.txn_max_base_fee_gwei,
        skip_base_fee_check=skip_base_fee_check,
        max_priority_fee_gwei=settings.txn_max_priority_fee_gwei,
        max_gas_limit=settings.txn_max_gas_limit,
        start_price_buffer_bps=settings.txn_start_price_buffer_bps,
        min_price_buffer_bps=settings.txn_min_price_buffer_bps,
        chain_id=settings.chain_id,
        confirm_fn=confirm_fn,
        require_curve_quote=resolved_require_curve_quote,
        erc20_reader=erc20_reader,
        auction_state_reader=auction_state_reader,
        pricing_policy=pricing_policy,
        token_sizing_policy=token_sizing_policy,
    )

    lock_path = settings.resolved_db_path.parent / "txn_daemon.lock"

    return TxnService(
        session=session,
        kicker=kicker,
        txn_run_repository=txn_run_repository,
        kick_tx_repository=kick_tx_repository,
        usd_threshold=settings.txn_usd_threshold,
        max_data_age_seconds=settings.txn_max_data_age_seconds,
        cooldown_seconds=settings.txn_cooldown_seconds,
        lock_path=lock_path,
        max_batch_kick_size=settings.max_batch_kick_size,
        batch_kick_delay_seconds=settings.batch_kick_delay_seconds,
    )
