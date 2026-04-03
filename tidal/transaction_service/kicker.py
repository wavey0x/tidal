"""Compatibility facade for kick preparation, tx building, and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import structlog

from tidal.time import utcnow_iso
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.chain.web3_client import Web3Client
from tidal.persistence.repositories import KickTxRepository
from tidal.pricing.token_price_agg import TokenPriceAggProvider
from tidal.scanner.auction_state import AuctionStateReader
from tidal.transaction_service.kick_execute import KickExecutor
from tidal.transaction_service.kick_policy import PricingPolicy, TokenSizingPolicy
from tidal.transaction_service.kick_prepare import KickPreparer
from tidal.transaction_service.kick_shared import (
    SelectedSellSize,
    _DEFAULT_PRIORITY_FEE_GWEI,
    _DEFAULT_STEP_DECAY_RATE_BPS,
    _GAS_ESTIMATE_BUFFER,
    _candidate_key,
    _clean_quote_response,
    _default_pricing_policy,
    _format_execution_error,
    _is_active_auction_error,
)
from tidal.transaction_service.kick_tx import KickTxBuilder
from tidal.transaction_service.signer import TransactionSigner
from tidal.transaction_service.types import KickCandidate, KickResult, KickStatus, PreparedKick, PreparedSweepAndSettle, TxIntent

logger = structlog.get_logger(__name__)


class AuctionKicker:
    """Compatibility facade over the split kick components."""

    def __init__(
        self,
        *,
        web3_client: Web3Client,
        signer: TransactionSigner | None,
        kick_tx_repository: KickTxRepository,
        price_provider: TokenPriceAggProvider,
        auction_kicker_address: str,
        usd_threshold: float,
        max_base_fee_gwei: float,
        max_priority_fee_gwei: int,
        skip_base_fee_check: bool = False,
        max_gas_limit: int,
        start_price_buffer_bps: int,
        min_price_buffer_bps: int,
        chain_id: int,
        confirm_fn: Callable[[dict], bool] | None = None,
        require_curve_quote: bool = True,
        default_step_decay_rate_bps: int = _DEFAULT_STEP_DECAY_RATE_BPS,
        quote_spot_warning_threshold_pct: float = 2.0,
        erc20_reader: ERC20Reader | None = None,
        auction_state_reader: AuctionStateReader | None = None,
        pricing_policy: PricingPolicy | None = None,
        token_sizing_policy: TokenSizingPolicy | None = None,
    ):
        self.web3_client = web3_client
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.price_provider = price_provider
        self.auction_kicker_address = auction_kicker_address
        self.usd_threshold = usd_threshold
        self.max_base_fee_gwei = max_base_fee_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.skip_base_fee_check = skip_base_fee_check
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.confirm_fn = confirm_fn
        self.require_curve_quote = require_curve_quote
        self.quote_spot_warning_threshold_pct = quote_spot_warning_threshold_pct
        self.erc20_reader = erc20_reader
        self.auction_state_reader = auction_state_reader
        self.pricing_policy = pricing_policy or _default_pricing_policy(
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            step_decay_rate_bps=default_step_decay_rate_bps,
        )
        self.token_sizing_policy = token_sizing_policy

        self.preparer = KickPreparer(
            web3_client=web3_client,
            price_provider=price_provider,
            usd_threshold=usd_threshold,
            require_curve_quote=require_curve_quote,
            erc20_reader=erc20_reader,
            auction_state_reader=auction_state_reader,
            pricing_policy=self.pricing_policy,
            token_sizing_policy=token_sizing_policy,
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            default_step_decay_rate_bps=default_step_decay_rate_bps,
            erc20_reader_factory=ERC20Reader,
            auction_state_reader_factory=AuctionStateReader,
            logger_instance=logger,
        )
        self.tx_builder = KickTxBuilder(
            web3_client=web3_client,
            auction_kicker_address=auction_kicker_address,
            chain_id=chain_id,
        )
        self.executor = KickExecutor(
            web3_client=web3_client,
            signer=signer,
            kick_tx_repository=kick_tx_repository,
            tx_builder=self.tx_builder,
            preparer=self,
            max_base_fee_gwei=max_base_fee_gwei,
            max_priority_fee_gwei=max_priority_fee_gwei,
            skip_base_fee_check=skip_base_fee_check,
            max_gas_limit=max_gas_limit,
            chain_id=chain_id,
            confirm_fn=confirm_fn,
            quote_spot_warning_threshold_pct=quote_spot_warning_threshold_pct,
            logger_instance=logger,
        )

    def _require_signer(self) -> TransactionSigner:
        return self.executor._require_signer()

    def _resolve_erc20_reader(self) -> ERC20Reader:
        return self.preparer._resolve_erc20_reader()

    def _resolve_auction_state_reader(self) -> AuctionStateReader:
        return self.preparer._resolve_auction_state_reader()

    async def _resolve_priority_fee_wei(self) -> int:
        return await self.executor._resolve_priority_fee_wei()

    def _select_sell_size(self, candidate: KickCandidate, live_balance_raw: int) -> SelectedSellSize:
        return self.preparer._select_sell_size(candidate, live_balance_raw)

    def _insert_operation_tx(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self.executor._insert_operation_tx(*args, **kwargs)

    def _fail(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self.executor._fail(*args, **kwargs)

    @staticmethod
    def _pk_audit_kwargs(prepared_kick: PreparedKick) -> dict[str, object]:
        return KickExecutor._pk_audit_kwargs(prepared_kick)

    async def _plan_recovery(self, prepared_kick: PreparedKick) -> PreparedKick | None:
        return await self.preparer._plan_recovery(prepared_kick)

    async def plan_recovery(self, prepared_kick: PreparedKick) -> PreparedKick | None:
        return await self._plan_recovery(prepared_kick)

    async def _estimate_transaction_data(
        self,
        *,
        tx_data: str,
        to_address: str,
        sender_address: str,
    ) -> tuple[int | None, str | None]:
        return await self.executor._estimate_transaction_data(
            tx_data=tx_data,
            to_address=to_address,
            sender_address=sender_address,
        )

    async def inspect_candidates(
        self,
        candidates: list[KickCandidate],
    ):
        return await self.preparer.inspect_candidates(candidates)

    async def _prepare_sweep_and_settle(
        self,
        candidate: KickCandidate,
        inspection,
    ) -> PreparedSweepAndSettle:
        return await self.preparer._prepare_sweep_and_settle(candidate, inspection)

    async def prepare_kick(
        self,
        candidate: KickCandidate,
        run_id: str,
        *,
        inspection=None,
    ) -> PreparedKick | PreparedSweepAndSettle | KickResult:
        return await self.preparer.prepare_kick(candidate, run_id, inspection=inspection)

    def _kicker_contract(self) -> tuple[str, object]:
        return self.tx_builder._kicker_contract()

    def build_single_kick_intent(self, prepared_kick: PreparedKick, *, sender: str | None) -> TxIntent:
        return self.tx_builder.build_single_kick_intent(prepared_kick, sender=sender)

    def build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        return self.tx_builder.build_batch_kick_intent(prepared_kicks, sender=sender)

    def build_sweep_and_settle_intent(
        self,
        prepared_operation: PreparedSweepAndSettle,
        *,
        sender: str | None,
    ) -> TxIntent:
        return self.tx_builder.build_sweep_and_settle_intent(prepared_operation, sender=sender)

    @staticmethod
    def _kick_args(prepared_kick: PreparedKick) -> tuple:
        return KickTxBuilder._kick_args(prepared_kick)

    @staticmethod
    def _kick_extended_args(prepared_kick: PreparedKick) -> tuple:
        return KickTxBuilder._kick_extended_args(prepared_kick)

    async def execute_batch(
        self,
        prepared_kicks: list[PreparedKick],
        run_id: str,
    ) -> list[KickResult]:
        return await self.executor.execute_batch(prepared_kicks, run_id)

    async def execute_single(
        self,
        prepared_kick: PreparedKick,
        run_id: str,
    ) -> KickResult:
        return await self.executor.execute_single(prepared_kick, run_id)

    async def execute_sweep_and_settle(
        self,
        prepared_operation: PreparedSweepAndSettle,
        run_id: str,
    ) -> KickResult:
        return await self.executor.execute_sweep_and_settle(prepared_operation, run_id)

    async def kick(self, candidate: KickCandidate, run_id: str) -> KickResult:
        result = await self.prepare_kick(candidate, run_id)
        if isinstance(result, KickResult):
            if result.status == KickStatus.SKIP:
                return result
            persisted = self._fail(
                run_id,
                candidate,
                utcnow_iso(),
                status=result.status,
                error_message=result.error_message or "candidate preparation failed",
                sell_amount=result.sell_amount,
                starting_price=result.starting_price,
                minimum_price=result.minimum_price,
                minimum_quote=result.minimum_quote,
                usd_value=result.usd_value,
                quote_response_json=result.quote_response_json,
            )
            return replace(
                persisted,
                live_balance_raw=result.live_balance_raw,
                tx_hash=result.tx_hash,
                gas_used=result.gas_used,
                gas_price_gwei=result.gas_price_gwei,
                block_number=result.block_number,
                quote_response_json=result.quote_response_json,
                execution_report=result.execution_report,
            )
        if isinstance(result, PreparedSweepAndSettle):
            return await self.execute_sweep_and_settle(result, run_id)
        return await self.execute_single(result, run_id)
