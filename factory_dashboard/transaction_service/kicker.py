"""Transaction builder and sender for auction kicks."""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_CEILING, Decimal

import structlog
from eth_utils import to_checksum_address

from factory_dashboard.chain.contracts.abis import AUCTION_KICKER_ABI
from factory_dashboard.chain.contracts.erc20 import ERC20Reader
from factory_dashboard.chain.web3_client import Web3Client
from factory_dashboard.constants import AUCTION_KICKER_ADDRESS
from factory_dashboard.normalizers import normalize_address, to_decimal_string
from factory_dashboard.persistence.repositories import KickTxRepository
from factory_dashboard.pricing.token_price_agg import TokenPriceAggProvider
from factory_dashboard.time import utcnow_iso
from factory_dashboard.transaction_service.signer import TransactionSigner
from factory_dashboard.transaction_service.types import KickCandidate, KickResult, KickStatus

logger = structlog.get_logger(__name__)

# Gas estimate buffer: 20% (hardcoded per spec).
_GAS_ESTIMATE_BUFFER = 1.2

# Fallback priority fee when the RPC call fails.
_DEFAULT_PRIORITY_FEE_GWEI = 0.1


class AuctionKicker:
    """Builds, signs, and sends kick transactions."""

    def __init__(
        self,
        *,
        web3_client: Web3Client,
        signer: TransactionSigner,
        kick_tx_repository: KickTxRepository,
        price_provider: TokenPriceAggProvider,
        usd_threshold: float,
        max_gas_price_gwei: int,
        max_priority_fee_gwei: int,
        max_gas_limit: int,
        start_price_buffer_bps: int,
        chain_id: int,
        confirm_fn: Callable[[dict], bool] | None = None,
    ):
        self.web3_client = web3_client
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.price_provider = price_provider
        self.usd_threshold = usd_threshold
        self.max_gas_price_gwei = max_gas_price_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.max_gas_limit = max_gas_limit
        self.start_price_buffer_bps = start_price_buffer_bps
        self.chain_id = chain_id
        self.confirm_fn = confirm_fn

    async def _resolve_priority_fee_wei(self) -> int:
        cap_wei = self.max_priority_fee_gwei * 10**9
        try:
            suggested_wei = await self.web3_client.get_max_priority_fee()
        except Exception:  # noqa: BLE001
            fallback_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 10**9)
            return min(fallback_wei, cap_wei)
        return min(suggested_wei, cap_wei)

    def _insert_kick_tx(
        self,
        run_id: str,
        candidate: KickCandidate,
        now_iso: str,
        *,
        status: KickStatus,
        error_message: str | None = None,
        sell_amount: str | None = None,
        starting_price: str | None = None,
        usd_value: str | None = None,
        tx_hash: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "strategy_address": candidate.strategy_address,
            "token_address": candidate.token_address,
            "auction_address": candidate.auction_address,
            "price_usd": candidate.price_usd,
            "status": status,
            "created_at": now_iso,
        }
        if error_message is not None:
            row["error_message"] = error_message
        if sell_amount is not None:
            row["sell_amount"] = sell_amount
        if starting_price is not None:
            row["starting_price"] = starting_price
        if usd_value is not None:
            row["usd_value"] = usd_value
        if tx_hash is not None:
            row["tx_hash"] = tx_hash
        return self.kick_tx_repository.insert(row)

    def _fail(
        self,
        run_id: str,
        candidate: KickCandidate,
        now_iso: str,
        *,
        status: KickStatus,
        error_message: str,
        sell_amount: str | None = None,
        starting_price: str | None = None,
        usd_value: str | None = None,
    ) -> KickResult:
        """Insert a kick_tx row and return a terminal KickResult."""
        kick_tx_id = self._insert_kick_tx(
            run_id, candidate, now_iso,
            status=status, error_message=error_message,
            sell_amount=sell_amount, starting_price=starting_price, usd_value=usd_value,
        )
        return KickResult(kick_tx_id=kick_tx_id, status=status, error_message=error_message)

    async def kick(self, candidate: KickCandidate, run_id: str) -> KickResult:
        """Execute the full kick flow for a single candidate."""

        now_iso = utcnow_iso()

        # 1. Re-read live balance on-chain.
        try:
            erc20 = ERC20Reader(self.web3_client)
            live_balance_raw = await erc20.read_balance(
                candidate.token_address, candidate.strategy_address
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message=f"balance read failed: {exc}",
            )

        # 2. Recalculate USD value with live balance.
        normalized_balance = to_decimal_string(live_balance_raw, candidate.decimals)
        live_usd_value = Decimal(normalized_balance) * Decimal(candidate.price_usd)

        if live_usd_value < self.usd_threshold:
            logger.info(
                "txn_candidate_below_threshold_live",
                strategy=candidate.strategy_address,
                token=candidate.token_address,
                cached_usd=candidate.usd_value,
                live_usd=live_usd_value,
            )
            # Below threshold on live read — log only, no kick_txs row per spec.
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="below threshold on live balance",
                live_balance_raw=live_balance_raw,
                usd_value=str(live_usd_value),
            )

        # 3. Fetch fresh sell→want quote for startingPrice.
        sell_amount = live_balance_raw
        try:
            quote_result = await self.price_provider.quote(
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                amount_in=str(sell_amount),
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message=f"quote API failed: {exc}",
            )

        if quote_result.amount_out_raw is None:
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message="no quote available for this pair",
            )

        amount_out_normalized = Decimal(to_decimal_string(quote_result.amount_out_raw, quote_result.token_out_decimals))
        buffer = Decimal(1) + Decimal(self.start_price_buffer_bps) / Decimal(10_000)
        starting_price_raw = int((amount_out_normalized * buffer).to_integral_value(rounding=ROUND_CEILING))
        sell_amount_str = str(sell_amount)
        starting_price_str = str(starting_price_raw)
        usd_value_str = str(live_usd_value)

        log_ctx = {
            "strategy": candidate.strategy_address,
            "token": candidate.token_address,
            "auction": candidate.auction_address,
            "sell_amount": sell_amount_str,
            "starting_price": starting_price_str,
            "usd_value": usd_value_str,
        }

        # 4. Check gas price.
        try:
            gas_price_wei = await self.web3_client.get_gas_price()
            gas_price_gwei = gas_price_wei / 1e9
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message=f"gas price check failed: {exc}",
                sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
            )

        if gas_price_gwei > self.max_gas_price_gwei:
            logger.warning("txn_safety_block", reason="gas_price_high", gas_gwei=gas_price_gwei, **log_ctx)
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR,
                error_message=f"gas price {gas_price_gwei:.1f} gwei exceeds ceiling {self.max_gas_price_gwei}",
                sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
            )

        # 5. Build transaction and estimateGas.
        kicker_contract = self.web3_client.contract(AUCTION_KICKER_ADDRESS, AUCTION_KICKER_ABI)
        kick_fn = kicker_contract.functions.kick(
            to_checksum_address(candidate.strategy_address),
            to_checksum_address(candidate.auction_address),
            to_checksum_address(candidate.token_address),
            sell_amount,
            starting_price_raw,
        )
        tx_data = kick_fn._encode_transaction_data()

        tx_params = {
            "from": self.signer.checksum_address,
            "to": to_checksum_address(AUCTION_KICKER_ADDRESS),
            "data": tx_data,
            "chainId": self.chain_id,
        }

        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:  # noqa: BLE001
            logger.info("txn_estimate_failed", error=str(exc), **log_ctx)
            kick_tx_id = self._insert_kick_tx(
                run_id, candidate, now_iso,
                status=KickStatus.ESTIMATE_FAILED, error_message=str(exc),
                sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
            )
            return KickResult(
                kick_tx_id=kick_tx_id,
                status=KickStatus.ESTIMATE_FAILED,
                error_message=str(exc),
                sell_amount=sell_amount_str,
                starting_price=starting_price_str,
                live_balance_raw=live_balance_raw,
                usd_value=usd_value_str,
            )

        # 6. Gas limit = min(estimate * 1.2, max_gas_limit).
        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), self.max_gas_limit)
        if gas_estimate > self.max_gas_limit:
            error_msg = f"gas estimate {gas_estimate} exceeds cap {self.max_gas_limit}"
            logger.warning("txn_safety_block", reason="gas_estimate_over_cap", **log_ctx)
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message=error_msg,
                sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
            )

        # 7. Interactive confirmation gate.
        if self.confirm_fn is not None:
            summary = {
                "strategy": candidate.strategy_address,
                "token": candidate.token_address,
                "auction": candidate.auction_address,
                "sell_amount": normalized_balance,
                "usd_value": usd_value_str,
                "starting_price": starting_price_str,
                "starting_price_display": f"{starting_price_raw} want-token units (lot value with {self.start_price_buffer_bps}bp buffer)",
                "sell_price_usd": candidate.price_usd,
                "want_address": candidate.want_address,
                "buffer_bps": self.start_price_buffer_bps,
                "gas_estimate": gas_estimate,
                "gas_limit": gas_limit,
            }
            if not self.confirm_fn(summary):
                kick_tx_id = self._insert_kick_tx(
                    run_id, candidate, now_iso,
                    status=KickStatus.USER_SKIPPED,
                    sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
                )
                return KickResult(
                    kick_tx_id=kick_tx_id,
                    status=KickStatus.USER_SKIPPED,
                    sell_amount=sell_amount_str,
                    starting_price=starting_price_str,
                    live_balance_raw=live_balance_raw,
                    usd_value=usd_value_str,
                )

        # 8. Sign + send.
        nonce = await self.web3_client.get_transaction_count(self.signer.address)

        priority_fee_wei = await self._resolve_priority_fee_wei()
        max_fee_wei = self.max_gas_price_gwei * 10**9

        full_tx = {
            "to": to_checksum_address(AUCTION_KICKER_ADDRESS),
            "data": tx_data,
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "nonce": nonce,
            "type": 2,
        }

        try:
            signed_tx = self.signer.sign_transaction(full_tx)
            tx_hash = await self.web3_client.send_raw_transaction(signed_tx)
        except Exception as exc:  # noqa: BLE001
            logger.error("txn_send_failed", error=str(exc), **log_ctx)
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message=f"send failed: {exc}",
                sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
            )

        # Persist SUBMITTED immediately after broadcast.
        kick_tx_id = self._insert_kick_tx(
            run_id, candidate, now_iso,
            status=KickStatus.SUBMITTED, tx_hash=tx_hash,
            sell_amount=sell_amount_str, starting_price=starting_price_str, usd_value=usd_value_str,
        )

        logger.info("txn_kick_submitted", tx_hash=tx_hash, kick_tx_id=kick_tx_id, **log_ctx)

        # 9. Wait for receipt.
        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:  # noqa: BLE001
            # Receipt timeout — row stays SUBMITTED, blocks future sends for this pair.
            logger.warning("txn_receipt_timeout", tx_hash=tx_hash, error=str(exc), **log_ctx)
            return KickResult(
                kick_tx_id=kick_tx_id,
                status=KickStatus.SUBMITTED,
                tx_hash=tx_hash,
                sell_amount=sell_amount_str,
                starting_price=starting_price_str,
                live_balance_raw=live_balance_raw,
                usd_value=usd_value_str,
                error_message=f"receipt timeout: {exc}",
            )

        # 10. Update to CONFIRMED or REVERTED.
        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None

        if receipt_status == 1:
            final_status = KickStatus.CONFIRMED
            logger.info(
                "txn_kick_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                **log_ctx,
            )
        else:
            final_status = KickStatus.REVERTED
            logger.warning("txn_kick_reverted", tx_hash=tx_hash, block_number=receipt_block, **log_ctx)

        self.kick_tx_repository.update_status(
            kick_tx_id,
            status=final_status,
            gas_used=receipt_gas_used,
            gas_price_gwei=effective_gwei,
            block_number=receipt_block,
        )

        return KickResult(
            kick_tx_id=kick_tx_id,
            status=final_status,
            tx_hash=tx_hash,
            gas_used=receipt_gas_used,
            gas_price_gwei=effective_gwei,
            block_number=receipt_block,
            sell_amount=sell_amount_str,
            starting_price=starting_price_str,
            live_balance_raw=live_balance_raw,
            usd_value=usd_value_str,
        )
