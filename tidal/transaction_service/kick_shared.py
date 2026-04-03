"""Shared helpers for kick preparation and execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from tidal.normalizers import normalize_address, short_address, to_decimal_string
from tidal.transaction_service.kick_policy import PricingPolicy, PricingProfile
from tidal.transaction_service.types import KickCandidate

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
    return token_in_symbol is not None and want_symbol is not None and token_in_symbol == want_symbol


def _candidate_symbol_matches_want(candidate: KickCandidate) -> bool:
    token_symbol = _normalize_symbol(candidate.token_symbol)
    want_symbol = _normalize_symbol(candidate.want_symbol)
    return token_symbol is not None and want_symbol is not None and token_symbol == want_symbol


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
            return f"panic 0x{code:x}: {reason}" if reason is not None else f"panic 0x{code:x}"
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
    return normalize_address(candidate.auction_address), normalize_address(candidate.token_address)


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


def _select_sell_size(
    *,
    token_sizing_policy,
    candidate: KickCandidate,
    live_balance_raw: int,
) -> SelectedSellSize:
    full_live_balance_normalized = to_decimal_string(live_balance_raw, candidate.decimals)
    price_usd = Decimal(candidate.price_usd)
    full_live_usd_value = Decimal(full_live_balance_normalized) * price_usd

    selected_sell_raw = live_balance_raw
    max_usd_per_kick: Decimal | None = None

    if token_sizing_policy is not None:
        max_usd_per_kick = token_sizing_policy.resolve(candidate.token_address)
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
