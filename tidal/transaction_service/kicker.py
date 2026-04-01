"""Transaction builder and sender for auction operations."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import structlog
from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from tidal.auction_settlement import decide_auction_settlement
from tidal.auction_price_units import (
    compute_minimum_price_scaled_1e18,
    compute_minimum_quote_unscaled,
    compute_starting_price_unscaled,
    format_buffer_pct,
    scaled_price_to_public_raw,
)
from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.chain.web3_client import Web3Client
from tidal.normalizers import normalize_address, short_address, to_decimal_string
from tidal.persistence.repositories import KickTxRepository
from tidal.pricing.token_price_agg import TokenPriceAggProvider
from tidal.scanner.auction_state import AuctionStateReader
from tidal.time import utcnow_iso
from tidal.transaction_service.auction_recovery import plan_prepared_kick_recovery
from tidal.transaction_service.kick_policy import (
    PricingPolicy,
    PricingProfile,
    TokenSizingPolicy,
)
from tidal.transaction_service.signer import TransactionSigner
from tidal.transaction_service.types import (
    AuctionInspection,
    KickCandidate,
    KickRecoveryPlan,
    KickResult,
    KickStatus,
    PreparedKick,
    PreparedSweepAndSettle,
    TransactionExecutionReport,
)

logger = structlog.get_logger(__name__)

_GAS_ESTIMATE_BUFFER = 1.2
_DEFAULT_PRIORITY_FEE_GWEI = 0.1
_DEFAULT_STEP_DECAY_RATE_BPS = 50

_ERROR_STRING_SELECTOR = keccak(text="Error(string)")[:4]
_PANIC_SELECTOR = keccak(text="Panic(uint256)")[:4]
_EXECUTION_FAILED_SELECTOR = keccak(text="ExecutionFailed(uint256,address,string)")[:4]
_PANIC_REASONS = {
    0x01: "assertion failed",
    0x11: "arithmetic overflow/underflow",
    0x12: "division or modulo by zero",
    0x21: "invalid enum conversion",
    0x22: "invalid storage byte array encoding",
    0x31: "pop on empty array",
    0x32: "array index out of bounds",
    0x41: "too much memory allocated",
    0x51: "zero-initialized internal function",
}


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


def _normalize_symbol(symbol: object) -> str | None:
    if symbol is None:
        return None

    normalized = "".join(ch.lower() for ch in str(symbol).strip() if ch.isalnum())
    return normalized or None


def _normalize_address_or_none(address: object) -> str | None:
    if address is None:
        return None

    try:
        return normalize_address(str(address))
    except Exception:
        return None


def _quote_metadata_resolves_to_want(candidate: KickCandidate, raw: dict | None) -> bool:
    if not isinstance(raw, dict):
        return False

    token_in = raw.get("token_in")
    if not isinstance(token_in, dict):
        return False

    token_in_address = _normalize_address_or_none(token_in.get("address"))
    want_address = _normalize_address_or_none(candidate.want_address)
    if token_in_address is not None and want_address is not None and token_in_address == want_address:
        return True

    token_in_symbol = _normalize_symbol(token_in.get("symbol"))
    want_symbol = _normalize_symbol(candidate.want_symbol)
    return (
        token_in_symbol is not None
        and want_symbol is not None
        and token_in_symbol == want_symbol
    )


def _candidate_symbol_matches_want(candidate: KickCandidate) -> bool:
    token_symbol = _normalize_symbol(candidate.token_symbol)
    want_symbol = _normalize_symbol(candidate.want_symbol)
    return (
        token_symbol is not None
        and want_symbol is not None
        and token_symbol == want_symbol
    )


def _walk_error_values(value: object) -> list[str]:
    values: list[str] = []

    def _walk(item: object) -> None:
        if isinstance(item, BaseException):
            _walk(item.args)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                _walk(child)
            return
        if not isinstance(item, str):
            return

        values.append(item)
        if item and item[0] in {"(", "["}:
            try:
                parsed = ast.literal_eval(item)
            except Exception:
                return
            _walk(parsed)

    _walk(value)
    return values


def _decode_revert_payload(payload: str) -> str | None:
    if not isinstance(payload, str) or not payload.startswith("0x") or len(payload) < 10:
        return None

    try:
        raw = HexBytes(payload)
    except Exception:
        return None

    selector = bytes(raw[:4])
    data = bytes(raw[4:])

    try:
        if selector == _ERROR_STRING_SELECTOR:
            return str(abi_decode(["string"], data)[0])

        if selector == _PANIC_SELECTOR:
            code = int(abi_decode(["uint256"], data)[0])
            reason = _PANIC_REASONS.get(code)
            if reason is not None:
                return f"panic 0x{code:x}: {reason}"
            return f"panic 0x{code:x}"

        if selector == _EXECUTION_FAILED_SELECTOR:
            _, target, message = abi_decode(["uint256", "address", "string"], data)
            target_address = to_checksum_address(target)
            return f"call to {short_address(target_address)} failed: {message}"
    except Exception:
        return None

    return None


def _format_execution_error(exc: Exception) -> str:
    decoded_messages: list[str] = []
    reverted_messages: list[str] = []

    for value in _walk_error_values(exc):
        decoded = _decode_revert_payload(value)
        if decoded and decoded not in decoded_messages:
            decoded_messages.append(decoded)
            continue

        marker = "execution reverted:"
        lowered = value.lower()
        if marker in lowered:
            idx = lowered.index(marker) + len(marker)
            reason = value[idx:].strip()
            if reason and reason not in reverted_messages:
                reverted_messages.append(reason)

    if decoded_messages:
        return decoded_messages[0]
    if reverted_messages:
        return reverted_messages[0]
    return str(exc)


def _is_active_auction_error(message: str | None) -> bool:
    return bool(message and "active auction" in message.lower())


def _candidate_key(candidate: KickCandidate) -> tuple[str, str]:
    return (
        normalize_address(candidate.auction_address),
        normalize_address(candidate.token_address),
    )


def _default_pricing_policy(
    *,
    start_price_buffer_bps: int,
    min_price_buffer_bps: int,
    step_decay_rate_bps: int,
) -> PricingPolicy:
    default_profile = PricingProfile(
        name="volatile",
        start_price_buffer_bps=start_price_buffer_bps,
        min_price_buffer_bps=min_price_buffer_bps,
        step_decay_rate_bps=step_decay_rate_bps,
    )
    return PricingPolicy(
        default_profile_name=default_profile.name,
        profiles={default_profile.name: default_profile},
        profile_overrides={},
    )


@dataclass(frozen=True, slots=True)
class SelectedSellSize:
    full_live_balance_raw: int
    full_live_balance_normalized: str
    full_live_usd_value: Decimal
    selected_sell_raw: int
    selected_sell_normalized: str
    selected_sell_usd_value: Decimal
    max_usd_per_kick: Decimal | None = None


class AuctionKicker:
    """Builds, signs, and sends kick and stuck-auction abort transactions."""

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
        self.usd_threshold = Decimal(str(usd_threshold))
        self.max_base_fee_gwei = max_base_fee_gwei
        self.max_priority_fee_gwei = max_priority_fee_gwei
        self.skip_base_fee_check = skip_base_fee_check
        self.max_gas_limit = max_gas_limit
        self.chain_id = chain_id
        self.confirm_fn = confirm_fn
        self.require_curve_quote = require_curve_quote
        self.quote_spot_warning_threshold_pct = Decimal(str(quote_spot_warning_threshold_pct))
        self.erc20_reader = erc20_reader
        self.auction_state_reader = auction_state_reader
        self.pricing_policy = pricing_policy or _default_pricing_policy(
            start_price_buffer_bps=start_price_buffer_bps,
            min_price_buffer_bps=min_price_buffer_bps,
            step_decay_rate_bps=default_step_decay_rate_bps,
        )
        self.token_sizing_policy = token_sizing_policy

    def _require_signer(self) -> TransactionSigner:
        if self.signer is None:
            raise RuntimeError("Signer is required for live execution.")
        return self.signer

    def _resolve_erc20_reader(self) -> ERC20Reader:
        if self.erc20_reader is not None:
            return self.erc20_reader
        return ERC20Reader(self.web3_client)

    def _resolve_auction_state_reader(self) -> AuctionStateReader:
        if self.auction_state_reader is not None:
            return self.auction_state_reader
        return AuctionStateReader(
            web3_client=self.web3_client,
            multicall_client=None,
            multicall_enabled=False,
            multicall_auction_batch_calls=1,
        )

    async def _resolve_priority_fee_wei(self) -> int:
        cap_wei = self.max_priority_fee_gwei * 10**9
        try:
            suggested_wei = await self.web3_client.get_max_priority_fee()
        except Exception:  # noqa: BLE001
            fallback_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 10**9)
            return min(fallback_wei, cap_wei)
        return min(suggested_wei, cap_wei)

    def _select_sell_size(self, candidate: KickCandidate, live_balance_raw: int) -> SelectedSellSize:
        full_live_balance_normalized = to_decimal_string(live_balance_raw, candidate.decimals)
        price_usd = Decimal(candidate.price_usd)
        full_live_usd_value = Decimal(full_live_balance_normalized) * price_usd

        selected_sell_raw = live_balance_raw
        max_usd_per_kick: Decimal | None = None

        if self.token_sizing_policy is not None:
            max_usd_per_kick = self.token_sizing_policy.resolve(candidate.token_address)
            if max_usd_per_kick is not None:
                if price_usd <= 0:
                    raise ValueError("cached token price must be positive for token sizing")
                usd_cap_raw = int(
                    ((max_usd_per_kick / price_usd) * (Decimal(10) ** candidate.decimals)).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                selected_sell_raw = min(live_balance_raw, max(usd_cap_raw, 0))

        selected_sell_normalized = to_decimal_string(selected_sell_raw, candidate.decimals)
        selected_sell_usd_value = Decimal(selected_sell_normalized) * price_usd

        return SelectedSellSize(
            full_live_balance_raw=live_balance_raw,
            full_live_balance_normalized=full_live_balance_normalized,
            full_live_usd_value=full_live_usd_value,
            selected_sell_raw=selected_sell_raw,
            selected_sell_normalized=selected_sell_normalized,
            selected_sell_usd_value=selected_sell_usd_value,
            max_usd_per_kick=max_usd_per_kick,
        )

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
        if token_address is None or normalize_address(token_address) == normalize_address(candidate.token_address):
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
        logger.debug(
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

    def _pk_audit_kwargs(self, pk: PreparedKick) -> dict[str, object]:
        return {
            "sell_amount": pk.sell_amount_str,
            "starting_price": pk.starting_price_str,
            "minimum_price": pk.minimum_price_str,
            "minimum_quote": pk.minimum_quote_str,
            "usd_value": pk.usd_value_str,
            "quote_amount": pk.quote_amount_str,
            "quote_response_json": pk.quote_response_json,
            "start_price_buffer_bps": pk.start_price_buffer_bps,
            "min_price_buffer_bps": pk.min_price_buffer_bps,
            "step_decay_rate_bps": pk.step_decay_rate_bps,
            "settle_token": pk.settle_token,
            "normalized_balance": pk.normalized_balance,
        }

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
        except Exception as exc:  # noqa: BLE001
            return None, _format_execution_error(exc)
        return int(gas_estimate), None

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
            active_tokens = tuple(sorted(
                token_address
                for token_address in tokens_to_probe_by_auction.get(auction_address, set())
                if token_active.get((auction_address, token_address)) is True
            ))
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
                except Exception:  # noqa: BLE001
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

        if inspection.active_available_raw is not None and normalize_address(sell_token) == normalize_address(candidate.token_address):
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
        """Validate a candidate and compute the next action."""

        now_iso = utcnow_iso()

        if candidate.token_address == candidate.want_address:
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="sell token matches want token",
            )

        if _candidate_symbol_matches_want(candidate):
            logger.info(
                "txn_candidate_skip_same_symbol",
                source=candidate.source_address,
                token=candidate.token_address,
                token_symbol=candidate.token_symbol,
                want_address=candidate.want_address,
                want_symbol=candidate.want_symbol,
            )
            return KickResult(
                kick_tx_id=0,
                status=KickStatus.SKIP,
                error_message="sell token symbol matches want token",
            )

        if inspection is None:
            inspection = (await self.inspect_candidates([candidate])).get(_candidate_key(candidate))

        settle_token: str | None = None
        if inspection is None:
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message="auction inspection missing",
            )

        if inspection.is_active_auction is None:
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message="auction isAnActiveAuction() read failed",
            )

        if inspection.is_active_auction is True:
            settlement_decision = decide_auction_settlement(inspection)
            if settlement_decision.status == "error":
                return self._fail(
                    run_id,
                    candidate,
                    now_iso,
                    status=KickStatus.ERROR,
                    error_message=settlement_decision.reason,
                )
            if settlement_decision.status == "actionable":
                if settlement_decision.operation_type == "settle":
                    settle_token = settlement_decision.token_address
                else:
                    return await self._prepare_sweep_and_settle(candidate, inspection)
            else:
                return KickResult(
                    kick_tx_id=0,
                    status=KickStatus.SKIP,
                    error_message=settlement_decision.reason,
                )

        try:
            live_balance_raw = await self._resolve_erc20_reader().read_balance(
                candidate.token_address,
                candidate.source_address,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"balance read failed: {exc}",
            )

        try:
            selected_sell = self._select_sell_size(candidate, live_balance_raw)
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"token sizing failed: {exc}",
            )

        if selected_sell.full_live_usd_value < self.usd_threshold:
            logger.info(
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
            logger.info(
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
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"quote API failed: {exc}",
            )

        if _quote_metadata_resolves_to_want(candidate, quote_result.raw_response):
            raw_token_in = quote_result.raw_response.get("token_in", {}) if isinstance(quote_result.raw_response, dict) else {}
            logger.info(
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
            logger.warning(
                "txn_quote_no_amount",
                source=candidate.source_address,
                token_in=candidate.token_address,
                token_out=candidate.want_address,
                provider_statuses=quote_result.provider_statuses,
                request_url=quote_result.request_url,
            )
            return self._fail(
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message="no quote available for this pair",
                quote_response_json=quote_response_json,
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
                run_id,
                candidate,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"curve quote unavailable (status: {curve_status})",
                quote_response_json=quote_response_json,
            )

        amount_out_normalized = Decimal(
            to_decimal_string(quote_result.amount_out_raw, quote_result.token_out_decimals)
        )
        starting_price_unscaled = compute_starting_price_unscaled(
            amount_out_raw=quote_result.amount_out_raw,
            want_decimals=quote_result.token_out_decimals,
            buffer_bps=profile.start_price_buffer_bps,
        )

        buffer = Decimal(1) + Decimal(profile.start_price_buffer_bps) / Decimal(10_000)
        exact_value = amount_out_normalized * buffer
        if exact_value > 0 and starting_price_unscaled > exact_value * 2:
            logger.warning(
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
        except Exception as exc:  # noqa: BLE001
            logger.info(
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
                pk.candidate,
                now_iso,
                status=status,
                error_message=error_message,
                **self._pk_audit_kwargs(pk),
            )
            for pk in prepared_kicks
        ]

    async def _execute_tx(
        self,
        prepared_kicks: list[PreparedKick],
        tx_data: bytes,
        kicker_address: str,
        run_id: str,
    ) -> list[KickResult]:
        now_iso = utcnow_iso()
        batch_size = len(prepared_kicks)
        signer = self._require_signer()

        try:
            base_fee_wei = await self.web3_client.get_base_fee()
            base_fee_gwei = base_fee_wei / 1e9
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            friendly_error = _format_execution_error(exc)
            logger.info("txn_batch_estimate_failed", error=friendly_error, batch_size=batch_size)
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
            for pk in prepared_kicks:
                want_sym = pk.candidate.want_symbol or "want-token"
                kick_summaries.append(
                    {
                        "source": pk.candidate.source_address,
                        "source_name": pk.candidate.source_name,
                        "source_type": pk.candidate.source_type,
                        "sender": signer.checksum_address,
                        "strategy": pk.candidate.source_address,
                        "strategy_name": pk.candidate.source_name,
                        "token": pk.candidate.token_address,
                        "token_symbol": pk.candidate.token_symbol,
                        "auction": pk.candidate.auction_address,
                        "sell_amount": pk.normalized_balance,
                        "usd_value": pk.usd_value_str,
                        "starting_price": pk.starting_price_str,
                        "starting_price_display": (
                            f"{pk.starting_price_unscaled:,} {want_sym} "
                            f"(+{format_buffer_pct(pk.start_price_buffer_bps)} buffer)"
                        ),
                        "minimum_price": pk.minimum_price_str,
                        "minimum_price_scaled_1e18": pk.minimum_price_scaled_1e18_str,
                        "minimum_quote": pk.minimum_quote_unscaled_str,
                        "minimum_quote_display": (
                            f"{pk.minimum_quote_unscaled:,} {want_sym} "
                            f"(-{format_buffer_pct(pk.min_price_buffer_bps)} buffer)"
                        ),
                        "minimum_price_display": f"{pk.minimum_price_scaled_1e18:,} (scaled 1e18 floor)",
                        "sell_price_usd": pk.candidate.price_usd,
                        "want_address": pk.candidate.want_address,
                        "want_symbol": pk.candidate.want_symbol,
                        "want_price_usd": pk.want_price_usd_str,
                        "quote_rate": pk.quote_rate,
                        "start_rate": pk.start_rate,
                        "floor_rate": pk.floor_rate,
                        "buffer_bps": pk.start_price_buffer_bps,
                        "min_buffer_bps": pk.min_price_buffer_bps,
                        "step_decay_rate_bps": pk.step_decay_rate_bps,
                        "pricing_profile_name": pk.pricing_profile_name,
                        "settle_token": pk.settle_token,
                        "quote_amount": pk.quote_amount_str,
                    }
                )

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
                "quote_spot_warning_threshold_pct": float(self.quote_spot_warning_threshold_pct),
            }

            if not self.confirm_fn(summary):
                results = []
                for pk in prepared_kicks:
                    kick_tx_id = self._insert_operation_tx(
                        run_id,
                        pk.candidate,
                        now_iso,
                        operation_type="kick",
                        status=KickStatus.USER_SKIPPED,
                        **self._pk_audit_kwargs(pk),
                    )
                    results.append(
                        KickResult(
                            kick_tx_id=kick_tx_id,
                            status=KickStatus.USER_SKIPPED,
                            sell_amount=pk.sell_amount_str,
                            starting_price=pk.starting_price_str,
                            minimum_price=pk.minimum_price_str,
                            minimum_quote=pk.minimum_quote_str,
                            live_balance_raw=pk.live_balance_raw,
                            usd_value=pk.usd_value_str,
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
        except Exception as exc:  # noqa: BLE001
            logger.error("txn_batch_send_failed", error=str(exc), batch_size=batch_size)
            return self._fail_batch(
                run_id,
                prepared_kicks,
                now_iso,
                status=KickStatus.ERROR,
                error_message=f"send failed: {exc}",
            )

        kick_tx_ids = []
        for pk in prepared_kicks:
            kick_tx_id = self._insert_operation_tx(
                run_id,
                pk.candidate,
                now_iso,
                operation_type="kick",
                status=KickStatus.SUBMITTED,
                tx_hash=tx_hash,
                **self._pk_audit_kwargs(pk),
            )
            kick_tx_ids.append(kick_tx_id)

        logger.info("txn_batch_submitted", tx_hash=tx_hash, batch_size=batch_size)

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
                    minimum_quote=pk.minimum_quote_str,
                    live_balance_raw=pk.live_balance_raw,
                    usd_value=pk.usd_value_str,
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
                for i, pk in enumerate(prepared_kicks)
            ]

        receipt_status = receipt.get("status", 0)
        receipt_gas_used = receipt.get("gasUsed")
        effective_gas_price = receipt.get("effectiveGasPrice")
        receipt_block = receipt.get("blockNumber")
        effective_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None

        final_status = KickStatus.CONFIRMED if receipt_status == 1 else KickStatus.REVERTED

        if final_status == KickStatus.CONFIRMED:
            logger.info(
                "txn_batch_confirmed",
                tx_hash=tx_hash,
                block_number=receipt_block,
                gas_used=receipt_gas_used,
                batch_size=batch_size,
            )
        else:
            logger.warning(
                "txn_batch_reverted",
                tx_hash=tx_hash,
                block_number=receipt_block,
                batch_size=batch_size,
            )

        results = []
        for i, pk in enumerate(prepared_kicks):
            self.kick_tx_repository.update_status(
                kick_tx_ids[i],
                status=final_status.value,
                gas_used=receipt_gas_used,
                gas_price_gwei=effective_gwei,
                block_number=receipt_block,
            )
            results.append(
                KickResult(
                    kick_tx_id=kick_tx_ids[i],
                    status=final_status,
                    tx_hash=tx_hash,
                    gas_used=receipt_gas_used,
                    gas_price_gwei=effective_gwei,
                    block_number=receipt_block,
                    sell_amount=pk.sell_amount_str,
                    starting_price=pk.starting_price_str,
                    minimum_price=pk.minimum_price_str,
                    minimum_quote=pk.minimum_quote_str,
                    live_balance_raw=pk.live_balance_raw,
                    usd_value=pk.usd_value_str,
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

    def _kicker_contract(self) -> tuple[str, object]:
        addr = to_checksum_address(self.auction_kicker_address)
        return addr, self.web3_client.contract(addr, AUCTION_KICKER_ABI)

    @staticmethod
    def _kick_args(pk: PreparedKick) -> tuple:
        return (
            to_checksum_address(pk.candidate.source_address),
            to_checksum_address(pk.candidate.auction_address),
            to_checksum_address(pk.candidate.token_address),
            pk.sell_amount,
            to_checksum_address(pk.candidate.want_address),
            pk.starting_price_unscaled,
            pk.minimum_price_scaled_1e18,
            pk.step_decay_rate_bps,
            to_checksum_address(pk.settle_token) if pk.settle_token else "0x0000000000000000000000000000000000000000",
        )

    @staticmethod
    def _kick_extended_args(pk: PreparedKick) -> tuple:
        plan = pk.recovery_plan or KickRecoveryPlan()
        return (
            (
                to_checksum_address(pk.candidate.source_address),
                to_checksum_address(pk.candidate.auction_address),
                to_checksum_address(pk.candidate.token_address),
                pk.sell_amount,
                to_checksum_address(pk.candidate.want_address),
                pk.starting_price_unscaled,
                pk.minimum_price_scaled_1e18,
                pk.step_decay_rate_bps,
                to_checksum_address(pk.settle_token) if pk.settle_token else "0x0000000000000000000000000000000000000000",
                [to_checksum_address(address) for address in plan.settle_after_start],
                [to_checksum_address(address) for address in plan.settle_after_min],
                [to_checksum_address(address) for address in plan.settle_after_decay],
            ),
        )

    async def execute_batch(
        self,
        prepared_kicks: list[PreparedKick],
        run_id: str,
    ) -> list[KickResult]:
        if len(prepared_kicks) == 1 or any(prepared_kick.recovery_plan is not None for prepared_kick in prepared_kicks):
            return [await self.execute_single(prepared_kick, run_id) for prepared_kick in prepared_kicks]

        signer = self._require_signer()
        kicker_address, kicker_contract = self._kicker_contract()
        kick_tuples = [self._kick_args(pk) for pk in prepared_kicks]
        tx_data = kicker_contract.functions.batchKick(kick_tuples)._encode_transaction_data()
        _, estimate_error = await self._estimate_transaction_data(
            tx_data=tx_data,
            to_address=kicker_address,
            sender_address=signer.checksum_address,
        )
        if _is_active_auction_error(estimate_error):
            results: list[KickResult] = []
            for prepared_kick in prepared_kicks:
                results.append(await self.execute_single(prepared_kick, run_id))
            return results
        return await self._execute_tx(prepared_kicks, tx_data, kicker_address, run_id)

    async def execute_single(
        self,
        prepared_kick: PreparedKick,
        run_id: str,
    ) -> KickResult:
        signer = self._require_signer()
        kicker_address, kicker_contract = self._kicker_contract()
        execution_kick = prepared_kick

        if execution_kick.recovery_plan is None:
            standard_tx_data = kicker_contract.functions.kick(*self._kick_args(prepared_kick))._encode_transaction_data()
            _, estimate_error = await self._estimate_transaction_data(
                tx_data=standard_tx_data,
                to_address=kicker_address,
                sender_address=signer.checksum_address,
            )
            if _is_active_auction_error(estimate_error):
                recovered = await self._plan_recovery(prepared_kick)
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

        if execution_kick.recovery_plan is not None:
            tx_data = kicker_contract.functions.kickExtended(*self._kick_extended_args(execution_kick))._encode_transaction_data()
        else:
            tx_data = kicker_contract.functions.kick(*self._kick_args(execution_kick))._encode_transaction_data()
        results = await self._execute_tx([execution_kick], tx_data, kicker_address, run_id)
        return results[0]

    async def execute_sweep_and_settle(
        self,
        prepared_operation: PreparedSweepAndSettle,
        run_id: str,
    ) -> KickResult:
        now_iso = utcnow_iso()
        signer = self._require_signer()
        kicker_address, kicker_contract = self._kicker_contract()

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
        except Exception as exc:  # noqa: BLE001
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

        tx_data = kicker_contract.functions.sweepAndSettle(
            to_checksum_address(prepared_operation.candidate.auction_address),
            to_checksum_address(prepared_operation.sell_token),
        )._encode_transaction_data()
        tx_params = {
            "from": signer.checksum_address,
            "to": kicker_address,
            "data": tx_data,
            "chainId": self.chain_id,
        }

        try:
            gas_estimate = await self.web3_client.estimate_gas(tx_params)
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
            token_address=prepared_operation.sell_token,
            token_symbol=prepared_operation.token_symbol,
            sell_amount=prepared_operation.sell_amount_str,
            minimum_price=prepared_operation.minimum_price_str,
            usd_value=prepared_operation.usd_value_str,
            normalized_balance=prepared_operation.normalized_balance,
            stuck_abort_reason=prepared_operation.stuck_abort_reason,
        )

        try:
            receipt = await self.web3_client.get_transaction_receipt(tx_hash, timeout_seconds=120)
        except Exception as exc:  # noqa: BLE001
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

    async def kick(self, candidate: KickCandidate, run_id: str) -> KickResult:
        result = await self.prepare_kick(candidate, run_id)
        if isinstance(result, KickResult):
            return result
        if isinstance(result, PreparedSweepAndSettle):
            return await self.execute_sweep_and_settle(result, run_id)
        return await self.execute_single(result, run_id)
