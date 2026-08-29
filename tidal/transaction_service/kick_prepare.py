"""Preparation and inspection for kick operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import structlog

from tidal.auction_price_units import (
    compute_minimum_price_scaled_1e18,
    compute_minimum_quote_unscaled,
    decode_starting_price_amount,
    encode_starting_price_raw,
    latent_terminal_full_lot_ask_raw,
)
from tidal.auction_versions import approved_auction_spec_for_version
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.normalizers import to_decimal_string
from tidal.scanner.auction_state import AuctionStateReader
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
from tidal.transaction_service.types import AuctionInspection, KickCandidate, KickResult, KickStatus, PreparedKick

logger = structlog.get_logger(__name__)


def _is_successful_quote_status(status: str | None) -> bool:
    if status is None:
        return True
    return status.lower() in {"ok", "success", "successful"}


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) // 2


def _select_floor_quote_amount_raw(*, quote_result, profile) -> int:
    high_amount = quote_result.amount_out_raw
    if (
        high_amount is None
        or high_amount <= 0
        or not bool(getattr(profile, "outlier_floor_enabled", False))
    ):
        return high_amount

    successful_amounts = [
        amount
        for provider, amount in quote_result.provider_amounts.items()
        if amount > 0 and _is_successful_quote_status(quote_result.provider_statuses.get(provider))
    ]
    non_high_amounts = [amount for amount in successful_amounts if amount < high_amount]
    if len(non_high_amounts) < 2:
        return high_amount

    threshold_bps = profile.min_price_buffer_bps
    matching_high_count = sum(1 for amount in successful_amounts if amount == high_amount)
    has_nearby_provider = matching_high_count > 1 or any(
        0 < abs(high_amount - amount) * 10_000 <= high_amount * threshold_bps
        for amount in successful_amounts
    )
    if has_nearby_provider:
        return high_amount

    return _median_int(non_high_amounts)


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

        fresh_versions = await reader.read_string_noargs_many(auction_addresses, "version")
        compatibility_by_address: dict[str, list[bool]] = {}
        for candidate in candidates:
            auction_address, token_address = _candidate_key(candidate)
            fresh_version = fresh_versions.get(auction_address)
            compatibility_error: str | None = None
            try:
                approved_auction_spec_for_version(candidate.auction_version)
            except ValueError as exc:
                compatibility_error = str(exc)
            if fresh_version is None:
                compatibility_error = "auction version() read failed"
            else:
                try:
                    approved_auction_spec_for_version(fresh_version)
                except ValueError as exc:
                    compatibility_error = str(exc)
            if compatibility_error is None and fresh_version != candidate.auction_version:
                compatibility_error = (
                    f"stored auction version {candidate.auction_version!r} does not match "
                    f"onchain version {fresh_version!r}"
                )
            compatibility_by_address.setdefault(auction_address, []).append(
                compatibility_error is None
            )

            inspections[(auction_address, token_address)] = AuctionInspection(
                auction_address=auction_address,
                is_active_auction=None,
                active_tokens=(),
                auction_version=fresh_version,
                compatibility_error=compatibility_error,
            )

        # A stale row for a shared auction must fail the whole address closed.
        # This keeps every shared-ABI read behind an unambiguous stored/fresh
        # version agreement for every candidate that would consume it.
        approved_addresses = sorted(
            auction_address
            for auction_address, checks in compatibility_by_address.items()
            if all(checks)
        )
        blocked_addresses = {
            auction_address
            for auction_address, checks in compatibility_by_address.items()
            if not all(checks)
        }
        for key, inspection in inspections.items():
            if key[0] in blocked_addresses and inspection.compatibility_error is None:
                inspection.compatibility_error = (
                    "conflicting stored auction versions for the same auction address"
                )
        if approved_addresses:
            active_flags, auction_lengths, step_durations = await asyncio.gather(
                reader.read_bool_noargs_many(approved_addresses, "isAnActiveAuction"),
                reader.read_uint_noargs_many(approved_addresses, "auctionLength"),
                reader.read_uint_noargs_many(approved_addresses, "stepDuration"),
            )
            for key, inspection in inspections.items():
                auction_address = key[0]
                if inspection.compatibility_error is not None:
                    continue
                inspection.is_active_auction = active_flags.get(auction_address)
                inspection.auction_length_seconds = auction_lengths.get(auction_address)
                inspection.step_duration_seconds = step_durations.get(auction_address)

        return inspections

    async def prepare_kick(
        self,
        candidate: KickCandidate,
        run_id: str,
        *,
        inspection: AuctionInspection | None = None,
    ) -> PreparedKick | KickResult:
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
        if inspection is None:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message="auction inspection missing")
        if inspection.compatibility_error is not None:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=f"auction compatibility check failed: {inspection.compatibility_error}",
            )
        if candidate.auction_version != inspection.auction_version:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=(
                    f"stored auction version {candidate.auction_version!r} does not match "
                    f"onchain version {inspection.auction_version!r}"
                ),
            )
        if inspection.is_active_auction is None:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="auction isAnActiveAuction() read failed",
            )

        if inspection.is_active_auction is True:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="auction still active",
            )

        if inspection.auction_length_seconds is None or inspection.auction_length_seconds <= 0:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="auction auctionLength() read failed or returned zero",
            )
        if inspection.step_duration_seconds is None or inspection.step_duration_seconds <= 0:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="auction stepDuration() read failed or returned zero",
            )

        try:
            auction_spec = approved_auction_spec_for_version(inspection.auction_version)
        except ValueError as exc:
            return KickResult(kick_tx_id=0, status=KickStatus.ERROR, error_message=str(exc))

        erc20_reader = self._resolve_erc20_reader()
        try:
            live_balance_raw, sell_decimals, want_decimals = await asyncio.gather(
                erc20_reader.read_balance(candidate.token_address, candidate.source_address),
                erc20_reader.read_decimals(candidate.token_address),
                erc20_reader.read_decimals(candidate.want_address),
            )
        except Exception as exc:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=f"live token read failed: {exc}",
            )
        if sell_decimals != candidate.decimals:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=(
                    f"sell token decimals mismatch: cached {candidate.decimals}, onchain {sell_decimals}"
                ),
            )
        if not 0 <= sell_decimals <= 18 or not 0 <= want_decimals <= 18:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="auction tokens must have between 0 and 18 decimals",
            )

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

        if quote_result.token_out_decimals is None:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message="quote response is missing want token decimals",
                quote_response_json=quote_response_json,
            )
        if quote_result.token_out_decimals != want_decimals:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=(
                    f"want token decimals mismatch: quote {quote_result.token_out_decimals}, "
                    f"onchain {want_decimals}"
                ),
                quote_response_json=quote_response_json,
            )

        amount_out_normalized = Decimal(to_decimal_string(quote_result.amount_out_raw, want_decimals))
        try:
            starting_price_raw = encode_starting_price_raw(
                amount_out_raw=quote_result.amount_out_raw,
                want_decimals=want_decimals,
                buffer_bps=profile.start_price_buffer_bps,
                encoding=auction_spec.starting_price_encoding,
            )
        except ValueError as exc:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=f"starting price encoding failed: {exc}",
                quote_response_json=quote_response_json,
            )

        buffer = Decimal(1) + Decimal(profile.start_price_buffer_bps) / Decimal(10_000)
        exact_value = amount_out_normalized * buffer
        decoded_start = decode_starting_price_amount(
            starting_price_raw,
            auction_spec.starting_price_encoding,
        )
        if exact_value > 0 and decoded_start > exact_value * 2:
            self.logger.warning(
                "txn_starting_price_precision_loss",
                source=candidate.source_address,
                token=candidate.token_address,
                exact_want_value=str(exact_value),
                ceiled_value=str(decoded_start),
            )

        floor_quote_amount_raw = _select_floor_quote_amount_raw(
            quote_result=quote_result,
            profile=profile,
        )
        minimum_price_scaled_1e18 = compute_minimum_price_scaled_1e18(
            amount_out_raw=floor_quote_amount_raw,
            want_decimals=want_decimals,
            sell_amount_raw=sell_amount,
            sell_decimals=sell_decimals,
            buffer_bps=profile.min_price_buffer_bps,
        )
        minimum_quote_unscaled = compute_minimum_quote_unscaled(
            minimum_price_scaled_1e18=minimum_price_scaled_1e18,
            sell_amount_raw=sell_amount,
            sell_decimals=sell_decimals,
        )

        sell_scaler = 10 ** (18 - sell_decimals)
        want_scaler = 10 ** (18 - want_decimals)
        minimum_full_lot_quote_raw = (
            sell_amount * sell_scaler * minimum_price_scaled_1e18
        ) // 10**18 // want_scaler
        if minimum_full_lot_quote_raw >= floor_quote_amount_raw:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="minimum full-lot quote is not below the trusted floor quote",
                quote_response_json=quote_response_json,
            )

        try:
            terminal_ask_raw = latent_terminal_full_lot_ask_raw(
                encoding=auction_spec.starting_price_encoding,
                starting_price_raw=starting_price_raw,
                sell_amount_raw=sell_amount,
                sell_decimals=sell_decimals,
                want_decimals=want_decimals,
                step_decay_rate_bps=profile.step_decay_rate_bps,
                step_duration_seconds=inspection.step_duration_seconds,
                auction_length_seconds=inspection.auction_length_seconds,
            )
        except ValueError as exc:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.ERROR,
                error_message=f"terminal ask calculation failed: {exc}",
                quote_response_json=quote_response_json,
            )
        if terminal_ask_raw > floor_quote_amount_raw:
            floor_quote_display = to_decimal_string(floor_quote_amount_raw, want_decimals)
            terminal_display = to_decimal_string(terminal_ask_raw, want_decimals)
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message=(
                    f"quote {floor_quote_display} {candidate.want_symbol or 'want-token'} is below "
                    f"latent terminal full-lot ask {terminal_display} "
                    f"{candidate.want_symbol or 'want-token'}"
                ),
                quote_response_json=quote_response_json,
            )

        want_price_usd_str: str | None = None
        try:
            want_price_quote = await self.price_provider.quote_usd(
                candidate.want_address,
                want_decimals,
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
            starting_price_encoding=auction_spec.starting_price_encoding,
            sell_amount=sell_amount,
            starting_price_raw=starting_price_raw,
            minimum_price_scaled_1e18=minimum_price_scaled_1e18,
            minimum_quote_unscaled=minimum_quote_unscaled,
            sell_amount_str=str(sell_amount),
            starting_price_raw_str=str(starting_price_raw),
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
            want_price_usd_str=want_price_usd_str,
        )
