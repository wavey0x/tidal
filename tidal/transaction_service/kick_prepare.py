"""Preparation and inspection for kick operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import structlog

from tidal.auction_settlement import decide_auction_settlement
from tidal.auction_price_units import (
    compute_minimum_price_scaled_1e18,
    compute_minimum_quote_unscaled,
    compute_starting_price_unscaled,
    scaled_price_to_public_raw,
)
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.normalizers import normalize_address, to_decimal_string
from tidal.scanner.auction_state import AuctionStateReader
from tidal.transaction_service.auction_recovery import plan_prepared_kick_recovery
from tidal.transaction_service.kick_policy import PricingPolicy, TokenSizingPolicy
from tidal.transaction_service.kick_shared import (
    _DEFAULT_STEP_DECAY_RATE_BPS,
    _candidate_key,
    _candidate_symbol_matches_want,
    _clean_quote_response,
    _default_pricing_policy,
    _quote_metadata_resolves_to_want,
    _select_sell_size,
)
from tidal.transaction_service.types import AuctionInspection, KickCandidate, KickResult, KickStatus, PreparedKick, PreparedSweepAndSettle

logger = structlog.get_logger(__name__)


class KickPreparer:
    """Build prepared kick operations from shortlisted candidates."""

    def __init__(
        self,
        *,
        web3_client,
        price_provider,
        usd_threshold: float,
        require_curve_quote: bool = True,
        erc20_reader: ERC20Reader | None = None,
        auction_state_reader: AuctionStateReader | None = None,
        pricing_policy: PricingPolicy | None = None,
        token_sizing_policy: TokenSizingPolicy | None = None,
        start_price_buffer_bps: int,
        min_price_buffer_bps: int,
        default_step_decay_rate_bps: int = _DEFAULT_STEP_DECAY_RATE_BPS,
        erc20_reader_factory: Callable[[Any], ERC20Reader] | None = None,
        auction_state_reader_factory: Callable[..., AuctionStateReader] | None = None,
        logger_instance=None,
    ) -> None:
        self.web3_client = web3_client
        self.price_provider = price_provider
        self.usd_threshold = Decimal(str(usd_threshold))
        self.require_curve_quote = require_curve_quote
        self.erc20_reader = erc20_reader
        self.auction_state_reader = auction_state_reader
        self.pricing_policy = pricing_policy or _default_pricing_policy(
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            step_decay_rate_bps=default_step_decay_rate_bps,
        )
        self.token_sizing_policy = token_sizing_policy
        self.erc20_reader_factory = erc20_reader_factory or ERC20Reader
        self.auction_state_reader_factory = auction_state_reader_factory or AuctionStateReader
        self.logger = logger_instance or logger

    def _resolve_erc20_reader(self) -> ERC20Reader:
        if self.erc20_reader is not None:
            return self.erc20_reader
        return self.erc20_reader_factory(self.web3_client)

    def _resolve_auction_state_reader(self) -> AuctionStateReader:
        if self.auction_state_reader is not None:
            return self.auction_state_reader
        return self.auction_state_reader_factory(
            web3_client=self.web3_client,
            multicall_client=None,
            multicall_enabled=False,
            multicall_auction_batch_calls=1,
        )

    def _select_sell_size(self, candidate: KickCandidate, live_balance_raw: int):
        return _select_sell_size(
            token_sizing_policy=self.token_sizing_policy,
            candidate=candidate,
            live_balance_raw=live_balance_raw,
        )

    async def _plan_recovery(self, prepared_kick: PreparedKick) -> PreparedKick | None:
        plan = await plan_prepared_kick_recovery(
            prepared_kick=prepared_kick,
            web3_client=self.web3_client,
            erc20_reader=self._resolve_erc20_reader(),
        )
        if plan is None or plan.is_empty:
            return None
        return replace(prepared_kick, recovery_plan=plan)

    async def plan_recovery(self, prepared_kick: PreparedKick) -> PreparedKick | None:
        return await self._plan_recovery(prepared_kick)

    async def inspect_candidates(
        self,
        candidates: list[KickCandidate],
    ) -> dict[tuple[str, str], AuctionInspection]:
        inspections: dict[tuple[str, str], AuctionInspection] = {}
        if not candidates:
            return inspections

        reader = self._resolve_auction_state_reader()
        candidate_keys = [_candidate_key(candidate) for candidate in candidates]
        auction_addresses = sorted({auction_address for auction_address, _ in candidate_keys})

        active_flags = await reader.read_bool_noargs_many(auction_addresses, "isAnActiveAuction")
        active_auctions = [auction_address for auction_address in auction_addresses if active_flags.get(auction_address) is True]

        enabled_tokens = {}
        if active_auctions:
            enabled_tokens = await reader.read_address_array_noargs_many(active_auctions, "getAllEnabledAuctions")

        tokens_to_probe_by_auction: dict[str, set[str]] = {auction_address: set() for auction_address in active_auctions}
        for auction_address, token_address in candidate_keys:
            if active_flags.get(auction_address) is True:
                tokens_to_probe_by_auction[auction_address].add(token_address)
        for auction_address in active_auctions:
            tokens_to_probe_by_auction[auction_address].update(enabled_tokens.get(auction_address, []))

        probe_pairs = sorted(
            (auction_address, token_address)
            for auction_address, token_addresses in tokens_to_probe_by_auction.items()
            for token_address in token_addresses
        )

        token_active = {}
        if probe_pairs:
            token_active = await reader.read_bool_arg_many(probe_pairs, "isActive")

        active_tokens_by_auction: dict[str, tuple[str, ...]] = {}
        active_pairs: list[tuple[str, str]] = []
        for auction_address in active_auctions:
            active_tokens = tuple(
                sorted(
                    token_address
                    for token_address in tokens_to_probe_by_auction.get(auction_address, set())
                    if token_active.get((auction_address, token_address)) is True
                )
            )
            active_tokens_by_auction[auction_address] = active_tokens
            active_pairs.extend((auction_address, token_address) for token_address in active_tokens)

        available_by_pair = {}
        price_by_pair = {}
        minimum_price_by_auction = {}
        want_by_auction = {}
        if active_pairs:
            available_by_pair, price_by_pair, minimum_price_by_auction, want_by_auction = await asyncio.gather(
                reader.read_uint_arg_many(active_pairs, "available"),
                reader.read_uint_arg_many(active_pairs, "price"),
                reader.read_uint_noargs_many(active_auctions, "minimumPrice"),
                reader.read_address_noargs_many(active_auctions, "want"),
            )
        elif active_auctions:
            minimum_price_by_auction, want_by_auction = await asyncio.gather(
                reader.read_uint_noargs_many(active_auctions, "minimumPrice"),
                reader.read_address_noargs_many(active_auctions, "want"),
            )

        want_addresses = sorted({address for address in want_by_auction.values() if address})
        want_decimals_by_address: dict[str, int | None] = {}
        if want_addresses:
            erc20_reader = self._resolve_erc20_reader()

            async def _read_decimals(address: str) -> tuple[str, int | None]:
                try:
                    return address, await erc20_reader.read_decimals(address)
                except Exception:
                    return address, None

            want_decimals_by_address = dict(await asyncio.gather(*(_read_decimals(address) for address in want_addresses)))

        for auction_address, token_address in candidate_keys:
            active_tokens = active_tokens_by_auction.get(auction_address, ())
            active_token = active_tokens[0] if len(active_tokens) == 1 else None
            want_address = want_by_auction.get(auction_address)
            want_decimals = want_decimals_by_address.get(want_address) if want_address else None
            minimum_price_scaled_1e18 = minimum_price_by_auction.get(auction_address)
            inspections[(auction_address, token_address)] = AuctionInspection(
                auction_address=auction_address,
                is_active_auction=active_flags.get(auction_address),
                active_tokens=active_tokens,
                active_token=active_token,
                active_available_raw=available_by_pair.get((auction_address, active_token)) if active_token else None,
                active_price_public_raw=price_by_pair.get((auction_address, active_token)) if active_token else None,
                minimum_price_scaled_1e18=minimum_price_scaled_1e18,
                minimum_price_public_raw=scaled_price_to_public_raw(minimum_price_scaled_1e18, want_decimals),
                want_address=want_address,
                want_decimals=want_decimals,
            )

        return inspections

    async def _prepare_sweep_and_settle(
        self,
        candidate: KickCandidate,
        inspection: AuctionInspection,
    ) -> PreparedSweepAndSettle:
        sell_token = inspection.active_token or candidate.token_address
        token_symbol = candidate.token_symbol if normalize_address(sell_token) == normalize_address(candidate.token_address) else None
        sell_amount_str = str(inspection.active_available_raw) if inspection.active_available_raw is not None else None
        minimum_price_scaled_1e18_str = (
            str(inspection.minimum_price_scaled_1e18)
            if inspection.minimum_price_scaled_1e18 is not None
            else None
        )
        minimum_price_public_str = (
            str(inspection.minimum_price_public_raw)
            if inspection.minimum_price_public_raw is not None
            else None
        )
        normalized_balance = None
        usd_value_str = None

        if (
            inspection.active_available_raw is not None
            and normalize_address(sell_token) == normalize_address(candidate.token_address)
        ):
            normalized_balance = to_decimal_string(inspection.active_available_raw, candidate.decimals)
            usd_value_str = str(Decimal(normalized_balance) * Decimal(candidate.price_usd))

        return PreparedSweepAndSettle(
            candidate=candidate,
            sell_token=sell_token,
            minimum_price_scaled_1e18=inspection.minimum_price_scaled_1e18,
            minimum_price_public_raw=inspection.minimum_price_public_raw,
            available_raw=inspection.active_available_raw,
            sell_amount_str=sell_amount_str,
            minimum_price_scaled_1e18_str=minimum_price_scaled_1e18_str,
            minimum_price_public_str=minimum_price_public_str,
            usd_value_str=usd_value_str,
            normalized_balance=normalized_balance,
            stuck_abort_reason="active auction price is at or below minimumPrice",
            token_symbol=token_symbol,
        )

    async def prepare_kick(
        self,
        candidate: KickCandidate,
        run_id: str,
        *,
        inspection: AuctionInspection | None = None,
    ) -> PreparedKick | PreparedSweepAndSettle | KickResult:
        del run_id
        if candidate.token_address == candidate.want_address:
            return KickResult(kick_tx_id=0, status=KickStatus.SKIP, error_message="sell token matches want token")

        if _candidate_symbol_matches_want(candidate):
            self.logger.info(
                "txn_candidate_skip_same_symbol",
                source=candidate.source_address,
                token=candidate.token_address,
                token_symbol=candidate.token_symbol,
                want_address=candidate.want_address,
                want_symbol=candidate.want_symbol,
            )
            return KickResult(kick_tx_id=0, status=KickStatus.SKIP, error_message="sell token symbol matches want token")

        if inspection is None:
            inspection = (await self.inspect_candidates([candidate])).get(_candidate_key(candidate))
        settle_token: str | None = None
        if inspection is None:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message="auction inspection missing")
        if inspection.is_active_auction is None:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="auction isAnActiveAuction() read failed",
            )

        if inspection.is_active_auction is True:
            settlement_decision = decide_auction_settlement(inspection)
            if settlement_decision.status == "error":
                return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message=settlement_decision.reason)
            if settlement_decision.status == "actionable":
                if settlement_decision.operation_type == "settle":
                    settle_token = settlement_decision.token_address
                else:
                    return await self._prepare_sweep_and_settle(candidate, inspection)
            else:
                return KickResult(kick_tx_id=0, status=KickStatus.SKIP, error_message=settlement_decision.reason)

        try:
            live_balance_raw = await self._resolve_erc20_reader().read_balance(
                candidate.token_address,
                candidate.source_address,
            )
        except Exception as exc:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message=f"balance read failed: {exc}")

        try:
            selected_sell = self._select_sell_size(candidate, live_balance_raw)
        except Exception as exc:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message=f"token sizing failed: {exc}")

        if selected_sell.full_live_usd_value < self.usd_threshold:
            self.logger.info(
                "txn_candidate_below_threshold_live",
                source=candidate.source_address,
                token=candidate.token_address,
                cached_usd=candidate.usd_value,
                live_usd=selected_sell.full_live_usd_value,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="below threshold on live balance",
                live_balance_raw=live_balance_raw,
                usd_value=str(selected_sell.full_live_usd_value),
            )

        profile = self.pricing_policy.resolve(candidate.auction_address, candidate.token_address)
        sell_amount = selected_sell.selected_sell_raw
        if sell_amount <= 0:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="token sizing cap rounds to zero",
                live_balance_raw=live_balance_raw,
                sell_amount=str(sell_amount),
                usd_value=str(selected_sell.selected_sell_usd_value),
            )

        if selected_sell.selected_sell_usd_value < self.usd_threshold:
            self.logger.info(
                "txn_candidate_below_threshold_after_sizing",
                source=candidate.source_address,
                token=candidate.token_address,
                full_live_usd=selected_sell.full_live_usd_value,
                selected_usd=selected_sell.selected_sell_usd_value,
                max_usd_per_kick=selected_sell.max_usd_per_kick,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="below threshold after token sizing cap",
                live_balance_raw=live_balance_raw,
                sell_amount=str(sell_amount),
                usd_value=str(selected_sell.selected_sell_usd_value),
            )

        try:
            quote_result = await self.price_provider.quote(
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                amount_in=str(sell_amount),
            )
        except Exception as exc:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message=f"quote API failed: {exc}")

        if _quote_metadata_resolves_to_want(candidate, quote_result.raw_response):
            raw_token_in = quote_result.raw_response.get("token_in", {}) if isinstance(quote_result.raw_response, dict) else {}
            self.logger.info(
                "txn_quote_resolves_to_want_skip",
                source=candidate.source_address,
                token=candidate.token_address,
                token_symbol=candidate.token_symbol,
                want_address=candidate.want_address,
                want_symbol=candidate.want_symbol,
                quote_token_in_address=raw_token_in.get("address"),
                quote_token_in_symbol=raw_token_in.get("symbol"),
                request_url=quote_result.request_url,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="sell token resolves to want token in quote API",
            )

        quote_response_json = None
        if quote_result.raw_response is not None:
            try:
                cleaned = _clean_quote_response(quote_result.raw_response, request_url=quote_result.request_url)
                quote_response_json = json.dumps(cleaned)
            except (TypeError, ValueError):
                pass

        if quote_result.amount_out_raw is None:
            self.logger.warning(
                "txn_quote_no_amount",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                provider_statuses=quote_result.provider_statuses,
                request_url=quote_result.request_url,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="no quote available for this pair",
                quote_response_json=quote_response_json,
            )

        if self.require_curve_quote and not quote_result.curve_quote_available():
            curve_status = quote_result.provider_statuses.get("curve", "not present")
            self.logger.warning(
                "txn_quote_curve_unavailable",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                curve_status=curve_status,
                provider_statuses=quote_result.provider_statuses,
                request_url=quote_result.request_url,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=f"curve quote unavailable (status: {curve_status})",
                quote_response_json=quote_response_json,
            )

        amount_out_normalized = Decimal(to_decimal_string(quote_result.amount_out_raw, quote_result.token_out_decimals))
        starting_price_unscaled = compute_starting_price_unscaled(
            amount_out_raw=quote_result.amount_out_raw,
            want_decimals=quote_result.token_out_decimals,
            buffer_bps=profile.start_price_buffer_bps,
        )

        buffer = Decimal(1) + Decimal(profile.start_price_buffer_bps) / Decimal(10_000)
        exact_value = amount_out_normalized * buffer
        if exact_value > 0 and starting_price_unscaled > exact_value * 2:
            self.logger.warning(
                "txn_starting_price_precision_loss",
                source=candidate.source_address,
                token=candidate.token_address,
                exact_want_value=str(exact_value),
                ceiled_value=starting_price_unscaled,
            )

        minimum_price_scaled_1e18 = compute_minimum_price_scaled_1e18(
            amount_out_raw=quote_result.amount_out_raw,
            want_decimals=quote_result.token_out_decimals,
            sell_amount_raw=sell_amount,
            sell_decimals=candidate.decimals,
            buffer_bps=profile.min_price_buffer_bps,
        )
        minimum_quote_unscaled = compute_minimum_quote_unscaled(
            minimum_price_scaled_1e18=minimum_price_scaled_1e18,
            sell_amount_raw=sell_amount,
            sell_decimals=candidate.decimals,
        )

        want_price_usd_str: str | None = None
        try:
            want_price_quote = await self.price_provider.quote_usd(
                candidate.want_address,
                quote_result.token_out_decimals or 18,
            )
        except Exception as exc:
            self.logger.info(
                "txn_want_price_lookup_failed",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                error=str(exc),
            )
        else:
            if want_price_quote.price_usd is not None:
                want_price_usd_str = str(want_price_quote.price_usd)

        return PreparedKick(
            candidate=candidate,
            sell_amount=sell_amount,
            starting_price_unscaled=starting_price_unscaled,
            minimum_price_scaled_1e18=minimum_price_scaled_1e18,
            minimum_quote_unscaled=minimum_quote_unscaled,
            sell_amount_str=str(sell_amount),
            starting_price_unscaled_str=str(starting_price_unscaled),
            minimum_price_scaled_1e18_str=str(minimum_price_scaled_1e18),
            minimum_quote_unscaled_str=str(minimum_quote_unscaled),
            usd_value_str=str(selected_sell.selected_sell_usd_value),
            live_balance_raw=live_balance_raw,
            normalized_balance=selected_sell.selected_sell_normalized,
            quote_amount_str=str(amount_out_normalized),
            quote_response_json=quote_response_json,
            start_price_buffer_bps=profile.start_price_buffer_bps,
            min_price_buffer_bps=profile.min_price_buffer_bps,
            step_decay_rate_bps=profile.step_decay_rate_bps,
            pricing_profile_name=profile.name,
            settle_token=settle_token,
            want_price_usd_str=want_price_usd_str,
        )
