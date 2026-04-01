"""Shared auction pricing unit helpers."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from tidal.normalizers import to_decimal_string

WAD = Decimal(10) ** 18
WAD_INT = 10**18


def normalized_token_amount(raw_amount: int, decimals: int) -> Decimal:
    return Decimal(to_decimal_string(raw_amount, decimals))


def format_buffer_pct(buffer_bps: int) -> str:
    return f"{Decimal(buffer_bps) / Decimal(100):.2f}%"


def compute_starting_price_unscaled(*, amount_out_raw: int, want_decimals: int, buffer_bps: int) -> int:
    quote_amount = normalized_token_amount(amount_out_raw, want_decimals)
    buffer = Decimal(1) + Decimal(buffer_bps) / Decimal(10_000)
    return int((quote_amount * buffer).to_integral_value(rounding=ROUND_CEILING))


def compute_floor_rate(
    *,
    amount_out_raw: int,
    want_decimals: int,
    sell_amount_raw: int,
    sell_decimals: int,
    buffer_bps: int,
) -> Decimal:
    quote_amount = normalized_token_amount(amount_out_raw, want_decimals)
    sell_amount = normalized_token_amount(sell_amount_raw, sell_decimals)
    if sell_amount <= 0:
        raise ValueError("sell amount must be positive")
    buffer = Decimal(1) - Decimal(buffer_bps) / Decimal(10_000)
    floor_rate = (quote_amount / sell_amount) * buffer
    return max(Decimal(0), floor_rate)


def compute_minimum_price_scaled_1e18(
    *,
    amount_out_raw: int,
    want_decimals: int,
    sell_amount_raw: int,
    sell_decimals: int,
    buffer_bps: int,
) -> int:
    floor_rate = compute_floor_rate(
        amount_out_raw=amount_out_raw,
        want_decimals=want_decimals,
        sell_amount_raw=sell_amount_raw,
        sell_decimals=sell_decimals,
        buffer_bps=buffer_bps,
    )
    return int((floor_rate * WAD).to_integral_value(rounding=ROUND_FLOOR))


def scaled_price_to_rate(minimum_price_scaled_1e18: int | None) -> Decimal | None:
    if minimum_price_scaled_1e18 is None:
        return None
    return Decimal(minimum_price_scaled_1e18) / WAD


def scaled_price_to_public_raw(minimum_price_scaled_1e18: int | None, want_decimals: int | None) -> int | None:
    if minimum_price_scaled_1e18 is None or want_decimals is None:
        return None
    if want_decimals < 0 or want_decimals > 18:
        raise ValueError("want decimals must be between 0 and 18")
    scaler = 10 ** (18 - want_decimals)
    return minimum_price_scaled_1e18 // scaler


def compute_minimum_quote_unscaled(
    *,
    minimum_price_scaled_1e18: int,
    sell_amount_raw: int,
    sell_decimals: int,
) -> int:
    sell_amount = normalized_token_amount(sell_amount_raw, sell_decimals)
    floor_rate = Decimal(minimum_price_scaled_1e18) / WAD
    return int((sell_amount * floor_rate).to_integral_value(rounding=ROUND_FLOOR))
