"""Transaction builder and sender for auction kicks."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import structlog
from eth_utils import to_checksum_address

from factory_dashboard.chain.contracts.abis import AUCTION_KICKER_ABI
from factory_dashboard.chain.contracts.erc20 import ERC20Reader
from factory_dashboard.chain.web3_client import Web3Client
from factory_dashboard.normalizers import to_decimal_string
from factory_dashboard.persistence.repositories import KickTxRepository
from factory_dashboard.pricing.token_price_agg import TokenPriceAggProvider
from factory_dashboard.time import utcnow_iso
from factory_dashboard.transaction_service.signer import TransactionSigner
from factory_dashboard.transaction_service.types import (
    KickCandidate,
    KickResult,
    KickStatus,
    PreparedKick,
)

logger = structlog.get_logger(__name__)

# Gas estimate buffer: 20% (hardcoded per spec).
_GAS_ESTIMATE_BUFFER = 1.2

# Fallback priority fee when the RPC call fails.
_DEFAULT_PRIORITY_FEE_GWEI = 0.1


def _clean_quote_response(raw: dict, *, request_url: str | None = None) -> dict:
    """Keep only the fields useful for the kick log UI."""
    cleaned = {}
    if "summary" in raw:
        cleaned["summary"] = raw["summary"]
    if "providers" in raw:
        cleaned["providers"] = raw["providers"]
    token_out = raw.get("token_out")
    if isinstance(token_out, dict) and "decimals" in token_out:
        cleaned["tokenOutDecimals"] = token_out["decimals"]
    if request_url:
        cleaned["requestUrl"] = request_url
    return cleaned


class AuctionKicker:
    """Builds, signs, and sends kick transactions."""

    def __init__(
        self,
        *,
        web3_client: Web3Client,
        signer: TransactionSigner,
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
        self.start_price_buffer_bps = start_price_buffer_bps
        self.min_price_buffer_bps = min_price_buffer_bps
        self.chain_id = chain_id
        self.confirm_fn = confirm_fn
        self.require_curve_quote = require_curve_quote

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
        minimum_price: str | None = None,
        usd_value: str | None = None,
        tx_hash: str | None = None,
        quote_amount: str | None = None,
        quote_response_json: str | None = None,
        start_price_buffer_bps: int | None = None,
        min_price_buffer_bps: int | None = None,
        normalized_balance: str | None = None,
    ) -> int:
        row: dict[str, object] = {
            "run_id": run_id,
            "source_type": candidate.source_type,
            "source_address": candidate.source_address,
            "token_address": candidate.token_address,
            "auction_address": candidate.auction_address,
            "price_usd": candidate.price_usd,
            "status": status,
            "created_at": now_iso,
            "token_symbol": candidate.token_symbol,
            "want_address": candidate.want_address,
            "want_symbol": candidate.want_symbol,
        }
        if candidate.source_type == "strategy":
            row["strategy_address"] = candidate.source_address
        if error_message is not None:
            row["error_message"] = error_message
        if sell_amount is not None:
            row["sell_amount"] = sell_amount
        if starting_price is not None:
            row["starting_price"] = starting_price
        if minimum_price is not None:
            row["minimum_price"] = minimum_price
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
        if normalized_balance is not None:
            row["normalized_balance"] = normalized_balance
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
        minimum_price: str | None = None,
        usd_value: str | None = None,
        quote_amount: str | None = None,
        quote_response_json: str | None = None,
        start_price_buffer_bps: int | None = None,
        min_price_buffer_bps: int | None = None,
        normalized_balance: str | None = None,
    ) -> KickResult:
        """Insert a kick_tx row and return a terminal KickResult."""
        kick_tx_id = self._insert_kick_tx(
            run_id, candidate, now_iso,
            status=status, error_message=error_message,
            sell_amount=sell_amount, starting_price=starting_price,
            minimum_price=minimum_price, usd_value=usd_value,
            quote_amount=quote_amount, quote_response_json=quote_response_json,
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            normalized_balance=normalized_balance,
        )
        logger.debug(
            "txn_candidate_failed",
            run_id=run_id,
            source=candidate.source_address,
            token=candidate.token_address,
            token_symbol=candidate.token_symbol,
            want_symbol=candidate.want_symbol,
            auction=candidate.auction_address,
            status=status.value,
            error_message=error_message,
        )
        return KickResult(kick_tx_id=kick_tx_id, status=status, error_message=error_message)

    def _pk_audit_kwargs(self, pk: PreparedKick) -> dict[str, object]:
        """Extract common audit + pricing kwargs from a PreparedKick."""
        return {
            "sell_amount": pk.sell_amount_str,
            "starting_price": pk.starting_price_str,
            "minimum_price": pk.minimum_price_str,
            "usd_value": pk.usd_value_str,
            "quote_amount": pk.quote_amount_str,
            "quote_response_json": pk.quote_response_json,
            "start_price_buffer_bps": self.start_price_buffer_bps,
            "min_price_buffer_bps": self.min_price_buffer_bps,
            "normalized_balance": pk.normalized_balance,
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
        """Fail all kicks in a batch with a shared error."""
        return [
            self._fail(
                run_id, pk.candidate, now_iso,
                status=status, error_message=error_message,
                **self._pk_audit_kwargs(pk),
            )
            for pk in prepared_kicks
        ]

    # ------------------------------------------------------------------
    # Phase 1: Prepare (per-candidate)
    # ------------------------------------------------------------------

    async def prepare_kick(
        self, candidate: KickCandidate, run_id: str,
    ) -> PreparedKick | KickResult:
        """Validate a candidate and compute prices. Returns PreparedKick on
        success or KickResult on failure/skip."""

        now_iso = utcnow_iso()

        # 1. Re-read live balance on-chain.
        try:
            erc20 = ERC20Reader(self.web3_client)
            live_balance_raw = await erc20.read_balance(
                candidate.token_address, candidate.source_address
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
                source=candidate.source_address,
                token=candidate.token_address,
                cached_usd=candidate.usd_value,
                live_usd=live_usd_value,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="below threshold on live balance",
                live_balance_raw=live_balance_raw,
                usd_value=str(live_usd_value),
            )

        # 3. Fetch fresh sell→want quote for startingPrice / minimumPrice.
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

        _quote_json = None
        if quote_result.raw_response is not None:
            try:
                _quote_json = json.dumps(_clean_quote_response(quote_result.raw_response, request_url=quote_result.request_url))
            except (TypeError, ValueError):
                pass

        if quote_result.amount_out_raw is None:
            logger.warning(
                "txn_quote_no_amount",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                provider_statuses=quote_result.provider_statuses,
                request_url=quote_result.request_url,
            )
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR, error_message="no quote available for this pair",
                quote_response_json=_quote_json,
            )

        if self.require_curve_quote and not quote_result.curve_quote_available():
            curve_status = quote_result.provider_statuses.get("curve", "not present")
            logger.warning(
                "txn_quote_curve_unavailable",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                curve_status=curve_status,
                provider_statuses=quote_result.provider_statuses,
                request_url=quote_result.request_url,
            )
            return self._fail(
                run_id, candidate, now_iso,
                status=KickStatus.ERROR,
                error_message=f"curve quote unavailable (status: {curve_status})",
                quote_response_json=_quote_json,
            )

        amount_out_normalized = Decimal(to_decimal_string(quote_result.amount_out_raw, quote_result.token_out_decimals))
        buffer = Decimal(1) + Decimal(self.start_price_buffer_bps) / Decimal(10_000)
        starting_price_raw = int((amount_out_normalized * buffer).to_integral_value(rounding=ROUND_CEILING))

        exact_value = amount_out_normalized * buffer
        if exact_value > 0 and starting_price_raw > exact_value * 2:
            logger.warning(
                "txn_starting_price_precision_loss",
                source=candidate.source_address,
                token=candidate.token_address,
                exact_want_value=str(exact_value),
                ceiled_value=starting_price_raw,
            )

        min_buffer = Decimal(1) - Decimal(self.min_price_buffer_bps) / Decimal(10_000)
        minimum_price_raw = max(0, int((amount_out_normalized * min_buffer).to_integral_value(rounding=ROUND_FLOOR)))

        quote_response_json = None
        if quote_result.raw_response is not None:
            try:
                cleaned = _clean_quote_response(quote_result.raw_response, request_url=quote_result.request_url)
                quote_response_json = json.dumps(cleaned)
            except (TypeError, ValueError):
                pass

        return PreparedKick(
            candidate=candidate,
            sell_amount=sell_amount,
            starting_price_raw=starting_price_raw,
            minimum_price_raw=minimum_price_raw,
            sell_amount_str=str(sell_amount),
            starting_price_str=str(starting_price_raw),
            minimum_price_str=str(minimum_price_raw),
            usd_value_str=str(live_usd_value),
            live_balance_raw=live_balance_raw,
            normalized_balance=normalized_balance,
            quote_amount_str=str(amount_out_normalized),
            quote_response_json=quote_response_json,
        )

    # ------------------------------------------------------------------
    # Phase 2: Execute (shared core + thin wrappers)
    # ------------------------------------------------------------------

    async def _execute_tx(
        self,
        prepared_kicks: list[PreparedKick],
        tx_data: bytes,
        kicker_address: str,
        run_id: str,
    ) -> list[KickResult]:
        """Send pre-encoded tx_data and handle gas, confirmation, signing,
        receipt waiting, and DB persistence.  Contract-function-agnostic."""

        now_iso = utcnow_iso()
        batch_size = len(prepared_kicks)

        # 1. Check base fee.
        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:  # noqa: BLE001
            return self._fail_batch(
                run_id, prepared_kicks, now_iso,
                status=KickStatus.ERROR, error_message=f"base fee check failed: {exc}",
            )

        if not self.skip_base_fee_check and base_fee_gwei > self.max_base_fee_gwei:
            return self._fail_batch(
                run_id, prepared_kicks, now_iso,
                status=KickStatus.ERROR,
                error_message=f"base fee {base_fee_gwei:.2f} gwei exceeds limit {self.max_base_fee_gwei}",
            )

        tx_params = {
            "from": self.signer.checksum_address,
            "to": kicker_address,
            "data": tx_data,
            "chainId": self.chain_id,
        }

        # 2. Estimate gas.
        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:  # noqa: BLE001
            logger.info("txn_batch_estimate_failed", error=str(exc), batch_size=batch_size)
            return self._fail_batch(
                run_id, prepared_kicks, now_iso,
                status=KickStatus.ESTIMATE_FAILED, error_message=str(exc),
            )

        batch_gas_cap = self.max_gas_limit * batch_size
        gas_limit = min(int(gas_estimate * _GAS_ESTIMATE_BUFFER), batch_gas_cap)
        if gas_estimate > batch_gas_cap:
            return self._fail_batch(
                run_id, prepared_kicks, now_iso,
                status=KickStatus.ERROR,
                error_message=f"gas estimate {gas_estimate} exceeds batch cap {batch_gas_cap}",
            )

        # 3. Resolve priority fee.
        priority_fee_wei = await self._resolve_priority_fee_wei()

        # 4. Interactive confirmation gate.
        if self.confirm_fn is not None:
            kick_summaries = []
            for pk in prepared_kicks:
                want_sym = pk.candidate.want_symbol or "want-token"
                buffer_pct = self.start_price_buffer_bps / 100
                min_buffer_pct = self.min_price_buffer_bps / 100
                kick_summaries.append({
                    "source": pk.candidate.source_address,
                    "source_name": pk.candidate.source_name,
                    "source_type": pk.candidate.source_type,
                    "strategy": pk.candidate.source_address,
                    "strategy_name": pk.candidate.source_name,
                    "token": pk.candidate.token_address,
                    "token_symbol": pk.candidate.token_symbol,
                    "auction": pk.candidate.auction_address,
                    "sell_amount": pk.normalized_balance,
                    "usd_value": pk.usd_value_str,
                    "starting_price": pk.starting_price_str,
                    "starting_price_display": f"{pk.starting_price_raw:,} {want_sym} (incl. {buffer_pct:.0f}% buffer)",
                    "minimum_price": pk.minimum_price_str,
                    "minimum_price_display": f"{pk.minimum_price_raw:,} {want_sym} (minus {min_buffer_pct:.0f}% buffer)",
                    "sell_price_usd": pk.candidate.price_usd,
                    "want_address": pk.candidate.want_address,
                    "want_symbol": pk.candidate.want_symbol,
                    "buffer_bps": self.start_price_buffer_bps,
                    "min_buffer_bps": self.min_price_buffer_bps,
                    "quote_amount": pk.quote_amount_str,
                })

            summary = {
                "kicks": kick_summaries,
                "batch_size": batch_size,
                "total_usd": str(sum(Decimal(pk.usd_value_str) for pk in prepared_kicks)),
                "gas_estimate": gas_estimate,
                "gas_limit": gas_limit,
                "base_fee_gwei": base_fee_gwei,
                "priority_fee_gwei": priority_fee_wei / 1e9,
                "max_fee_per_gas_gwei": max(self.max_base_fee_gwei, base_fee_gwei) + self.max_priority_fee_gwei,
                "gas_cost_eth": gas_estimate * base_fee_gwei / 1e9,
            }

            if not self.confirm_fn(summary):
                results = []
                for pk in prepared_kicks:
                    kick_tx_id = self._insert_kick_tx(
                        run_id, pk.candidate, now_iso,
                        status=KickStatus.USER_SKIPPED,
                        **self._pk_audit_kwargs(pk),
                    )
                    results.append(KickResult(
                        kick_tx_id=kick_tx_id,
                        status=KickStatus.USER_SKIPPED,
                        sell_amount=pk.sell_amount_str,
                        starting_price=pk.starting_price_str,
                        minimum_price=pk.minimum_price_str,
                        live_balance_raw=pk.live_balance_raw,
                        usd_value=pk.usd_value_str,
                    ))
                return results

        # 5. Sign + send.
        nonce = await self.web3_client.get_transaction_count(self.signer.address)
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
            signed_tx = self.signer.sign_transaction(full_tx)
            tx_hash = await self.web3_client.send_raw_transaction(signed_tx)
        except Exception as exc:  # noqa: BLE001
            logger.error("txn_batch_send_failed", error=str(exc), batch_size=batch_size)
            return self._fail_batch(
                run_id, prepared_kicks, now_iso,
                status=KickStatus.ERROR, error_message=f"send failed: {exc}",
            )

        # 6. Persist SUBMITTED rows (all share the same tx_hash).
        kick_tx_ids = []
        for pk in prepared_kicks:
            kick_tx_id = self._insert_kick_tx(
                run_id, pk.candidate, now_iso,
                status=KickStatus.SUBMITTED, tx_hash=tx_hash,
                **self._pk_audit_kwargs(pk),
            )
            kick_tx_ids.append(kick_tx_id)

        logger.info("txn_batch_submitted", tx_hash=tx_hash, batch_size=batch_size)

        # 7. Wait for receipt.
        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:  # noqa: BLE001
            logger.warning("txn_batch_receipt_timeout", tx_hash=tx_hash, error=str(exc))
            return [
                KickResult(
                    kick_tx_id=kick_tx_ids[i],
                    status=KickStatus.SUBMITTED,
                    tx_hash=tx_hash,
                    sell_amount=pk.sell_amount_str,
                    starting_price=pk.starting_price_str,
                    minimum_price=pk.minimum_price_str,
                    live_balance_raw=pk.live_balance_raw,
                    usd_value=pk.usd_value_str,
                    error_message=f"receipt timeout: {exc}",
                )
                for i, pk in enumerate(prepared_kicks)
            ]

        # 8. Update all rows to CONFIRMED or REVERTED.
        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None

        final_status = KickStatus.CONFIRMED if receipt_status == 1 else KickStatus.REVERTED

        if final_status == KickStatus.CONFIRMED:
            logger.info("txn_batch_confirmed", tx_hash=tx_hash, block_number=receipt_block, gas_used=receipt_gas_used, batch_size=batch_size)
        else:
            logger.warning("txn_batch_reverted", tx_hash=tx_hash, block_number=receipt_block, batch_size=batch_size)

        results = []
        for i, pk in enumerate(prepared_kicks):
            self.kick_tx_repository.update_status(
                kick_tx_ids[i],
                status=final_status,
                gas_used=receipt_gas_used,
                gas_price_gwei=effective_gwei,
                block_number=receipt_block,
            )
            results.append(KickResult(
                kick_tx_id=kick_tx_ids[i],
                status=final_status,
                tx_hash=tx_hash,
                gas_used=receipt_gas_used,
                gas_price_gwei=effective_gwei,
                block_number=receipt_block,
                sell_amount=pk.sell_amount_str,
                starting_price=pk.starting_price_str,
                minimum_price=pk.minimum_price_str,
                live_balance_raw=pk.live_balance_raw,
                usd_value=pk.usd_value_str,
            ))
        return results

    def _kicker_contract(self) -> tuple:
        """Return (checksum_address, contract_instance) for the AuctionKicker."""
        addr = to_checksum_address(self.auction_kicker_address)
        return addr, self.web3_client.contract(addr, AUCTION_KICKER_ABI)

    @staticmethod
    def _kick_args(pk: PreparedKick) -> tuple:
        """Extract the 7 positional args shared by kick() and batchKick()."""
        return (
            to_checksum_address(pk.candidate.source_address),
            to_checksum_address(pk.candidate.auction_address),
            to_checksum_address(pk.candidate.token_address),
            pk.sell_amount,
            to_checksum_address(pk.candidate.want_address),
            pk.starting_price_raw,
            pk.minimum_price_raw,
        )

    async def execute_batch(
        self, prepared_kicks: list[PreparedKick], run_id: str,
    ) -> list[KickResult]:
        """Send a batch of prepared kicks in a single batchKick transaction."""

        kicker_address, kicker_contract = self._kicker_contract()
        kick_tuples = [self._kick_args(pk) for pk in prepared_kicks]

        tx_data = kicker_contract.functions.batchKick(kick_tuples)._encode_transaction_data()
        return await self._execute_tx(prepared_kicks, tx_data, kicker_address, run_id)

    async def execute_single(
        self, prepared_kick: PreparedKick, run_id: str,
    ) -> KickResult:
        """Send a single prepared kick as an individual kick() transaction."""

        kicker_address, kicker_contract = self._kicker_contract()
        tx_data = kicker_contract.functions.kick(
            *self._kick_args(prepared_kick),
        )._encode_transaction_data()

        results = await self._execute_tx([prepared_kick], tx_data, kicker_address, run_id)
        return results[0]

    # ------------------------------------------------------------------
    # Convenience wrapper (single-kick API)
    # ------------------------------------------------------------------

    async def kick(self, candidate: KickCandidate, run_id: str) -> KickResult:
        """Execute the full kick flow for a single candidate."""
        result = await self.prepare_kick(candidate, run_id)
        if isinstance(result, KickResult):
            return result
        return await self.execute_single(result, run_id)
