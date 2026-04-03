"""Live execution and persistence for kick operations."""

from __future__ import annotations

from decimal import Decimal

import structlog

from tidal.auction_price_units import format_buffer_pct
from tidal.time import utcnow_iso
from tidal.transaction_service.kick_shared import (
    _DEFAULT_PRIORITY_FEE_GWEI,
    _GAS_ESTIMATE_BUFFER,
    _format_execution_error,
    _is_active_auction_error,
)
from tidal.transaction_service.types import KickCandidate, KickResult, KickStatus, PreparedKick, PreparedSweepAndSettle, TransactionExecutionReport

logger = structlog.get_logger(__name__)


class KickExecutor:
    """Execute prepared kick operations and persist outcomes."""

    def __init__(
        self,
        *,
        web3_client,
        signer,
        kick_tx_repository,
        tx_builder,
        preparer=None,
        max_base_fee_gwei: float,
        max_priority_fee_gwei: int,
        skip_base_fee_check: bool = False,
        max_gas_limit: int,
        chain_id: int,
        confirm_fn=None,
        quote_spot_warning_threshold_pct: float = 2.0,
        logger_instance=None,
    ) -> None:
        self.web3_client = web3_client
        self.signer = signer
        self.kick_tx_repository = kick_tx_repository
        self.tx_builder = tx_builder
        self.preparer = preparer
        self.max_base_fee_gwei = max_base_fee_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.skip_base_fee_check = skip_base_fee_check
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.confirm_fn = confirm_fn
        self.quote_spot_warning_threshold_pct = Decimal(str(quote_spot_warning_threshold_pct))
        self.logger = logger_instance or logger

    def _require_signer(self):
        if self.signer is None:
            raise RuntimeError("Signer is required for live execution.")
        return self.signer

    async def _resolve_priority_fee_wei(self) -> int:
        cap_wei = self.max_priority_fee_gwei * 10**9
        try:
            suggested_wei = await self.web3_client.get_max_priority_fee()
        except Exception:
            fallback_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 10**9)
            return min(fallback_wei, cap_wei)
        return min(suggested_wei, cap_wei)

    def _insert_operation_tx(
        self,
        run_id: str,
        candidate: KickCandidate,
        now_iso: str,
        *,
        operation_type: str,
        status: KickStatus | str,
        error_message: str | None = None,
        token_address: str | None = None,
        token_symbol: str | None = None,
        sell_amount: str | None = None,
        starting_price: str | None = None,
        minimum_price: str | None = None,
        minimum_quote: str | None = None,
        usd_value: str | None = None,
        tx_hash: str | None = None,
        quote_amount: str | None = None,
        quote_response_json: str | None = None,
        start_price_buffer_bps: int | None = None,
        min_price_buffer_bps: int | None = None,
        step_decay_rate_bps: int | None = None,
        settle_token: str | None = None,
        normalized_balance: str | None = None,
        stuck_abort_reason: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "operation_type": operation_type,
            "source_type": candidate.source_type,
            "source_address": candidate.source_address,
            "token_address": token_address or candidate.token_address,
            "auction_address": candidate.auction_address,
            "status": status.value if isinstance(status, KickStatus) else status,
            "created_at": now_iso,
            "token_symbol": token_symbol if token_symbol is not None else candidate.token_symbol,
            "want_address": candidate.want_address,
            "want_symbol": candidate.want_symbol,
        }
        if candidate.source_type == "strategy":
            row["strategy_address"] = candidate.source_address
        if token_address is None or token_address.lower() == candidate.token_address.lower():
            row["price_usd"] = candidate.price_usd
        if error_message is not None:
            row["error_message"] = error_message
        if sell_amount is not None:
            row["sell_amount"] = sell_amount
        if starting_price is not None:
            row["starting_price"] = starting_price
        if minimum_price is not None:
            row["minimum_price"] = minimum_price
        if minimum_quote is not None:
            row["minimum_quote"] = minimum_quote
        if usd_value is not None:
            row["usd_value"] = usd_value
        if tx_hash is not None:
            row["tx_hash"] = tx_hash
        if quote_amount is not None:
            row["quote_amount"] = quote_amount
        if quote_response_json is not None:
            row["quote_response_json"] = quote_response_json
        if start_price_buffer_bps is not None:
            row["start_price_buffer_bps"] = start_price_buffer_bps
        if min_price_buffer_bps is not None:
            row["min_price_buffer_bps"] = min_price_buffer_bps
        if step_decay_rate_bps is not None:
            row["step_decay_rate_bps"] = step_decay_rate_bps
        if settle_token is not None:
            row["settle_token"] = settle_token
        if normalized_balance is not None:
            row["normalized_balance"] = normalized_balance
        if stuck_abort_reason is not None:
            row["stuck_abort_reason"] = stuck_abort_reason
        return self.kick_tx_repository.insert(row)

    def _fail(
        self,
        run_id: str,
        candidate: KickCandidate,
        now_iso: str,
        *,
        status: KickStatus,
        error_message: str,
        operation_type: str = "kick",
        token_address: str | None = None,
        token_symbol: str | None = None,
        sell_amount: str | None = None,
        starting_price: str | None = None,
        minimum_price: str | None = None,
        minimum_quote: str | None = None,
        usd_value: str | None = None,
        quote_amount: str | None = None,
        quote_response_json: str | None = None,
        start_price_buffer_bps: int | None = None,
        min_price_buffer_bps: int | None = None,
        step_decay_rate_bps: int | None = None,
        settle_token: str | None = None,
        normalized_balance: str | None = None,
        stuck_abort_reason: str | None = None,
    ) -> KickResult:
        kick_tx_id = self._insert_operation_tx(
            run_id,
            candidate,
            now_iso,
            operation_type=operation_type,
            status=status,
            error_message=error_message,
            token_address=token_address,
            token_symbol=token_symbol,
            sell_amount=sell_amount,
            starting_price=starting_price,
            minimum_price=minimum_price,
            minimum_quote=minimum_quote,
            usd_value=usd_value,
            quote_amount=quote_amount,
            quote_response_json=quote_response_json,
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            step_decay_rate_bps=step_decay_rate_bps,
            settle_token=settle_token,
            normalized_balance=normalized_balance,
            stuck_abort_reason=stuck_abort_reason,
        )
        self.logger.debug(
            "txn_candidate_failed",
            run_id=run_id,
            source=candidate.source_address,
            token=token_address or candidate.token_address,
            token_symbol=token_symbol or candidate.token_symbol,
            want_symbol=candidate.want_symbol,
            auction=candidate.auction_address,
            operation_type=operation_type,
            status=status.value,
            error_message=error_message,
        )
        return KickResult(kick_tx_id=kick_tx_id, status=status, error_message=error_message)

    @staticmethod
    def _pk_audit_kwargs(prepared_kick: PreparedKick) -> dict[str, object]:
        return {
            "sell_amount": prepared_kick.sell_amount_str,
            "starting_price": prepared_kick.starting_price_str,
            "minimum_price": prepared_kick.minimum_price_str,
            "minimum_quote": prepared_kick.minimum_quote_str,
            "usd_value": prepared_kick.usd_value_str,
            "quote_amount": prepared_kick.quote_amount_str,
            "quote_response_json": prepared_kick.quote_response_json,
            "start_price_buffer_bps": prepared_kick.start_price_buffer_bps,
            "min_price_buffer_bps": prepared_kick.min_price_buffer_bps,
            "step_decay_rate_bps": prepared_kick.step_decay_rate_bps,
            "settle_token": prepared_kick.settle_token,
            "normalized_balance": prepared_kick.normalized_balance,
        }

    def _fail_batch(
        self,
        run_id: str,
        prepared_kicks: list[PreparedKick],
        now_iso: str,
        *,
        status: KickStatus,
        error_message: str,
    ) -> list[KickResult]:
        return [
            self._fail(
                run_id,
                prepared_kick.candidate,
                now_iso,
                status=status,
                error_message=error_message,
                **self._pk_audit_kwargs(prepared_kick),
            )
            for prepared_kick in prepared_kicks
        ]

    async def _estimate_transaction_data(
        self,
        *,
        tx_data: str,
        to_address: str,
        sender_address: str,
    ) -> tuple[int | None, str | None]:
        try:
            gas_estimate = await self.web3_client.estimate_gas(
                {
                    "from": sender_address,
                    "to": to_address,
                    "data": tx_data,
                    "chainId": self.chain_id,
                }
            )
        except Exception as exc:
            return None, _format_execution_error(exc)
        return int(gas_estimate), None

    async def _execute_tx(
        self,
        prepared_kicks: list[PreparedKick],
        tx_data: str,
        run_id: str,
    ) -> list[KickResult]:
        now_iso = utcnow_iso()
        batch_size = len(prepared_kicks)
        signer = self._require_signer()
        kicker_address, _ = self.tx_builder._kicker_contract()

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"base fee check failed: {exc}",
            )

        if not self.skip_base_fee_check and base_fee_gwei > self.max_base_fee_gwei:
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"base fee {base_fee_gwei:.2f} gwei exceeds limit {self.max_base_fee_gwei}",
            )

        tx_params = {
            "from": signer.checksum_address,
            "to": kicker_address,
            "data": tx_data,
            "chainId": self.chain_id,
        }
        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:
            friendly_error = _format_execution_error(exc)
            self.logger.info("txn_batch_estimate_failed", error=friendly_error, batch_size=batch_size)
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ESTIMATE_FAILED,
                error_message=friendly_error,
            )

        batch_gas_cap = self.max_gas_limit * batch_size
        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), batch_gas_cap)
        if gas_estimate > batch_gas_cap:
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"gas estimate {gas_estimate} exceeds batch cap {batch_gas_cap}",
            )

        priority_fee_wei = await self._resolve_priority_fee_wei()

        if self.confirm_fn is not None:
            kick_summaries = []
            for prepared_kick in prepared_kicks:
                want_symbol = prepared_kick.candidate.want_symbol or "want-token"
                kick_summaries.append(
                    {
                        "source": prepared_kick.candidate.source_address,
                        "source_name": prepared_kick.candidate.source_name,
                        "source_type": prepared_kick.candidate.source_type,
                        "sender": signer.checksum_address,
                        "strategy": prepared_kick.candidate.source_address,
                        "strategy_name": prepared_kick.candidate.source_name,
                        "token": prepared_kick.candidate.token_address,
                        "token_symbol": prepared_kick.candidate.token_symbol,
                        "auction": prepared_kick.candidate.auction_address,
                        "sell_amount": prepared_kick.normalized_balance,
                        "usd_value": prepared_kick.usd_value_str,
                        "starting_price": prepared_kick.starting_price_str,
                        "starting_price_display": (
                            f"{prepared_kick.starting_price_unscaled:,} {want_symbol} "
                            f"(+{format_buffer_pct(prepared_kick.start_price_buffer_bps)} buffer)"
                        ),
                        "minimum_price": prepared_kick.minimum_price_str,
                        "minimum_price_scaled_1e18": prepared_kick.minimum_price_scaled_1e18_str,
                        "minimum_quote": prepared_kick.minimum_quote_unscaled_str,
                        "minimum_quote_display": (
                            f"{prepared_kick.minimum_quote_unscaled:,} {want_symbol} "
                            f"(-{format_buffer_pct(prepared_kick.min_price_buffer_bps)} buffer)"
                        ),
                        "minimum_price_display": (
                            f"{prepared_kick.minimum_price_scaled_1e18:,} (scaled 1e18 floor)"
                        ),
                        "sell_price_usd": prepared_kick.candidate.price_usd,
                        "want_address": prepared_kick.candidate.want_address,
                        "want_symbol": prepared_kick.candidate.want_symbol,
                        "want_price_usd": prepared_kick.want_price_usd_str,
                        "quote_rate": prepared_kick.quote_rate,
                        "start_rate": prepared_kick.start_rate,
                        "floor_rate": prepared_kick.floor_rate,
                        "buffer_bps": prepared_kick.start_price_buffer_bps,
                        "min_buffer_bps": prepared_kick.min_price_buffer_bps,
                        "step_decay_rate_bps": prepared_kick.step_decay_rate_bps,
                        "pricing_profile_name": prepared_kick.pricing_profile_name,
                        "settle_token": prepared_kick.settle_token,
                        "quote_amount": prepared_kick.quote_amount_str,
                    }
                )
            summary = {
                "kicks": kick_summaries,
                "batch_size": batch_size,
                "total_usd": str(sum(Decimal(prepared_kick.usd_value_str) for prepared_kick in prepared_kicks)),
                "gas_estimate": gas_estimate,
                "gas_limit": gas_limit,
                "base_fee_gwei": base_fee_gwei,
                "priority_fee_gwei": priority_fee_wei / 1e9,
                "max_fee_per_gas_gwei": max(self.max_base_fee_gwei, base_fee_gwei) + self.max_priority_fee_gwei,
                "gas_cost_eth": gas_estimate * base_fee_gwei / 1e9,
                "quote_spot_warning_threshold_pct": float(self.quote_spot_warning_threshold_pct),
            }
            if not self.confirm_fn(summary):
                results = []
                for prepared_kick in prepared_kicks:
                    kick_tx_id = self._insert_operation_tx(
                        run_id,
                        prepared_kick.candidate,
                        now_iso,
                        operation_type="kick",
                        status=KickStatus.USER_SKIPPED,
                        **self._pk_audit_kwargs(prepared_kick),
                    )
                    results.append(
                        KickResult(
                            kick_tx_id=kick_tx_id,
                            status=KickStatus.USER_SKIPPED,
                            sell_amount=prepared_kick.sell_amount_str,
                            starting_price=prepared_kick.starting_price_str,
                            minimum_price=prepared_kick.minimum_price_str,
                            minimum_quote=prepared_kick.minimum_quote_str,
                            live_balance_raw=prepared_kick.live_balance_raw,
                            usd_value=prepared_kick.usd_value_str,
                        )
                    )
                return results

        nonce = await self.web3_client.get_transaction_count(signer.address)
        max_fee_wei = int((max(self.max_base_fee_gwei, base_fee_gwei) + self.max_priority_fee_gwei) * 10**9)
        full_tx = {
            "to": kicker_address,
            "data": tx_data,
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "nonce": nonce,
            "type": 2,
        }
        try:
            signed_tx = signer.sign_transaction(full_tx)
            tx_hash = await self.web3_client.send_raw_transaction(signed_tx)
        except Exception as exc:
            self.logger.error("txn_batch_send_failed", error=str(exc), batch_size=batch_size)
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"send failed: {exc}",
            )

        kick_tx_ids = []
        for prepared_kick in prepared_kicks:
            kick_tx_id = self._insert_operation_tx(
                run_id,
                prepared_kick.candidate,
                now_iso,
                operation_type="kick",
                status=KickStatus.SUBMITTED,
                tx_hash=tx_hash,
                **self._pk_audit_kwargs(prepared_kick),
            )
            kick_tx_ids.append(kick_tx_id)

        self.logger.info("txn_batch_submitted", tx_hash=tx_hash, batch_size=batch_size)

        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:
            self.logger.warning("txn_batch_receipt_timeout", tx_hash=tx_hash, error=str(exc))
            return [
                KickResult(
                    kick_tx_id=kick_tx_ids[index],
                    status=KickStatus.SUBMITTED,
                    tx_hash=tx_hash,
                    sell_amount=prepared_kick.sell_amount_str,
                    starting_price=prepared_kick.starting_price_str,
                    minimum_price=prepared_kick.minimum_price_str,
                    minimum_quote=prepared_kick.minimum_quote_str,
                    live_balance_raw=prepared_kick.live_balance_raw,
                    usd_value=prepared_kick.usd_value_str,
                    error_message=f"receipt timeout: {exc}",
                    execution_report=TransactionExecutionReport(
                        operation="kick",
                        sender=signer.checksum_address,
                        tx_hash=tx_hash,
                        broadcast_at=now_iso,
                        chain_id=self.chain_id,
                        gas_estimate=gas_estimate,
                    ),
                )
                for index, prepared_kick in enumerate(prepared_kicks)
            ]

        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
        final_status = KickStatus.CONFIRMED if receipt_status == 1 else KickStatus.REVERTED

        if final_status == KickStatus.CONFIRMED:
            self.logger.info(
                "txn_batch_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                batch_size=batch_size,
            )
        else:
            self.logger.warning(
                "txn_batch_reverted",
                tx_hash=tx_hash,
                block_number=receipt_block,
                batch_size=batch_size,
            )

        results = []
        for index, prepared_kick in enumerate(prepared_kicks):
            self.kick_tx_repository.update_status(
                kick_tx_ids[index],
                status=final_status.value,
                gas_used=receipt_gas_used,
                gas_price_gwei=effective_gwei,
                block_number=receipt_block,
            )
            results.append(
                KickResult(
                    kick_tx_id=kick_tx_ids[index],
                    status=final_status,
                    tx_hash=tx_hash,
                    gas_used=receipt_gas_used,
                    gas_price_gwei=effective_gwei,
                    block_number=receipt_block,
                    sell_amount=prepared_kick.sell_amount_str,
                    starting_price=prepared_kick.starting_price_str,
                    minimum_price=prepared_kick.minimum_price_str,
                    minimum_quote=prepared_kick.minimum_quote_str,
                    live_balance_raw=prepared_kick.live_balance_raw,
                    usd_value=prepared_kick.usd_value_str,
                    execution_report=TransactionExecutionReport(
                        operation="kick",
                        sender=signer.checksum_address,
                        tx_hash=tx_hash,
                        broadcast_at=now_iso,
                        chain_id=self.chain_id,
                        gas_estimate=gas_estimate,
                        receipt_status=final_status.value,
                        block_number=receipt_block,
                        gas_used=receipt_gas_used,
                    ),
                )
            )
        return results

    async def execute_batch(
        self,
        prepared_kicks: list[PreparedKick],
        run_id: str,
    ) -> list[KickResult]:
        if len(prepared_kicks) == 1 or any(prepared_kick.recovery_plan is not None for prepared_kick in prepared_kicks):
            return [await self.execute_single(prepared_kick, run_id) for prepared_kick in prepared_kicks]

        signer = self._require_signer()
        batch_intent = self.tx_builder.build_batch_kick_intent(prepared_kicks, sender=signer.checksum_address)
        _, estimate_error = await self._estimate_transaction_data(
            tx_data=batch_intent.data,
            to_address=batch_intent.to,
            sender_address=signer.checksum_address,
        )
        if _is_active_auction_error(estimate_error):
            return [await self.execute_single(prepared_kick, run_id) for prepared_kick in prepared_kicks]
        return await self._execute_tx(prepared_kicks, batch_intent.data, run_id)

    async def execute_single(
        self,
        prepared_kick: PreparedKick,
        run_id: str,
    ) -> KickResult:
        signer = self._require_signer()
        execution_kick = prepared_kick

        if execution_kick.recovery_plan is None:
            standard_intent = self.tx_builder.build_single_kick_intent(prepared_kick, sender=signer.checksum_address)
            _, estimate_error = await self._estimate_transaction_data(
                tx_data=standard_intent.data,
                to_address=standard_intent.to,
                sender_address=signer.checksum_address,
            )
            if _is_active_auction_error(estimate_error):
                recovered = await self.preparer.plan_recovery(prepared_kick) if self.preparer is not None else None
                if recovered is not None:
                    execution_kick = recovered
                else:
                    return self._fail(
                        run_id,
                        prepared_kick.candidate,
                        utcnow_iso(),
                        status=KickStatus.ESTIMATE_FAILED,
                        error_message=estimate_error or "active auction",
                        **self._pk_audit_kwargs(prepared_kick),
                    )

        intent = self.tx_builder.build_single_kick_intent(execution_kick, sender=signer.checksum_address)
        results = await self._execute_tx([execution_kick], intent.data, run_id)
        return results[0]

    async def execute_sweep_and_settle(
        self,
        prepared_operation: PreparedSweepAndSettle,
        run_id: str,
    ) -> KickResult:
        now_iso = utcnow_iso()
        signer = self._require_signer()
        op_kwargs = {
            "token_address": prepared_operation.sell_token,
            "token_symbol": prepared_operation.token_symbol,
            "sell_amount": prepared_operation.sell_amount_str,
            "minimum_price": prepared_operation.minimum_price_str,
            "usd_value": prepared_operation.usd_value_str,
            "normalized_balance": prepared_operation.normalized_balance,
            "stuck_abort_reason": prepared_operation.stuck_abort_reason,
        }

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:
            return self._fail(
                run_id,
                prepared_operation.candidate,
                now_iso,
                operation_type="sweep_and_settle",
                status=KickStatus.ERROR,
                error_message=f"base fee check failed: {exc}",
                **op_kwargs,
            )

        if not self.skip_base_fee_check and base_fee_gwei > self.max_base_fee_gwei:
            return self._fail(
                run_id,
                prepared_operation.candidate,
                now_iso,
                operation_type="sweep_and_settle",
                status=KickStatus.ERROR,
                error_message=f"base fee {base_fee_gwei:.2f} gwei exceeds limit {self.max_base_fee_gwei}",
                **op_kwargs,
            )

        intent = self.tx_builder.build_sweep_and_settle_intent(prepared_operation, sender=signer.checksum_address)
        try:
            gas_estimate = await self.web3_client.estimate_gas(
                {
                    "from": signer.checksum_address,
                    "to": intent.to,
                    "data": intent.data,
                    "chainId": self.chain_id,
                }
            )
        except Exception as exc:
            friendly_error = _format_execution_error(exc)
            return self._fail(
                run_id,
                prepared_operation.candidate,
                now_iso,
                operation_type="sweep_and_settle",
                status=KickStatus.ESTIMATE_FAILED,
                error_message=friendly_error,
                **op_kwargs,
            )

        if gas_estimate > self.max_gas_limit:
            return self._fail(
                run_id,
                prepared_operation.candidate,
                now_iso,
                operation_type="sweep_and_settle",
                status=KickStatus.ERROR,
                error_message=f"gas estimate {gas_estimate} exceeds batch cap {self.max_gas_limit}",
                **op_kwargs,
            )

        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), self.max_gas_limit)
        priority_fee_wei = await self._resolve_priority_fee_wei()
        nonce = await self.web3_client.get_transaction_count(signer.address)
        max_fee_wei = int((max(self.max_base_fee_gwei, base_fee_gwei) + self.max_priority_fee_gwei) * 10**9)
        full_tx = {
            "to": intent.to,
            "data": intent.data,
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "nonce": nonce,
            "type": 2,
        }

        try:
            signed_tx = signer.sign_transaction(full_tx)
            tx_hash = await self.web3_client.send_raw_transaction(signed_tx)
        except Exception as exc:
            return self._fail(
                run_id,
                prepared_operation.candidate,
                now_iso,
                operation_type="sweep_and_settle",
                status=KickStatus.ERROR,
                error_message=f"send failed: {exc}",
                **op_kwargs,
            )

        kick_tx_id = self._insert_operation_tx(
            run_id,
            prepared_operation.candidate,
            now_iso,
            operation_type="sweep_and_settle",
            status=KickStatus.SUBMITTED,
            tx_hash=tx_hash,
            **op_kwargs,
        )

        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:
            return KickResult(
                kick_tx_id=kick_tx_id,
                status=KickStatus.SUBMITTED,
                tx_hash=tx_hash,
                sell_amount=prepared_operation.sell_amount_str,
                minimum_price=prepared_operation.minimum_price_str,
                usd_value=prepared_operation.usd_value_str,
                error_message=f"receipt timeout: {exc}",
                execution_report=TransactionExecutionReport(
                    operation="sweep-and-settle",
                    sender=signer.checksum_address,
                    tx_hash=tx_hash,
                    broadcast_at=now_iso,
                    chain_id=self.chain_id,
                    gas_estimate=gas_estimate,
                ),
            )

        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
        final_status = KickStatus.CONFIRMED if receipt_status == 1 else KickStatus.REVERTED

        self.kick_tx_repository.update_status(
            kick_tx_id,
            status=final_status.value,
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
            sell_amount=prepared_operation.sell_amount_str,
            minimum_price=prepared_operation.minimum_price_str,
            usd_value=prepared_operation.usd_value_str,
            execution_report=TransactionExecutionReport(
                operation="sweep-and-settle",
                sender=signer.checksum_address,
                tx_hash=tx_hash,
                broadcast_at=now_iso,
                chain_id=self.chain_id,
                gas_estimate=gas_estimate,
                receipt_status=final_status.value,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
            ),
        )
