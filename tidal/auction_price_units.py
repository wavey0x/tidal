"""Shared auction pricing unit helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from tidal.auction_versions import StartingPriceEncoding
from tidal.normalizers import to_decimal_string

WAD = Decimal(10) ** 18
WAD_INT = 10**18


def normalized_token_amount(raw_amount: int, decimals: int) -> Decimal:
    return Decimal(to_decimal_string(raw_amount, decimals))


def format_buffer_pct(buffer_bps: int) -> str:
    return f"{Decimal(buffer_bps) / Decimal(100):.2f}%"


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (numerator + denominator - 1) // denominator


def encode_starting_price_raw(
    *,
    amount_out_raw: int,
    want_decimals: int,
    buffer_bps: int,
    encoding: StartingPriceEncoding,
) -> int:
    if amount_out_raw <= 0:
        raise ValueError("quote amount must be positive")
    if not 0 <= want_decimals <= 18:
        raise ValueError("want decimals must be between 0 and 18")
    if buffer_bps < 0:
        raise ValueError("starting price buffer must be non-negative")

    buffered_numerator = amount_out_raw * (10_000 + buffer_bps)
    if encoding == StartingPriceEncoding.WHOLE_WANT:
        encoded = _ceil_div(buffered_numerator, 10_000 * 10**want_decimals)
    elif encoding == StartingPriceEncoding.WAD_WANT:
        encoded = _ceil_div(buffered_numerator * 10 ** (18 - want_decimals), 10_000)
    else:
        raise ValueError(f"unsupported starting price encoding: {encoding}")

    if encoded <= 0:
        raise ValueError("encoded starting price must be positive")
    if encoding == StartingPriceEncoding.WAD_WANT and encoded <= 10**9:
        raise ValueError("v1.0.5 starting price must be greater than 1e9")
    return encoded


def decode_starting_price_amount(raw_value: int, encoding: StartingPriceEncoding) -> Decimal:
    if raw_value <= 0:
        raise ValueError("starting price must be positive")
    if encoding == StartingPriceEncoding.WHOLE_WANT:
        return Decimal(raw_value)
    if encoding == StartingPriceEncoding.WAD_WANT:
        return Decimal(raw_value) / WAD
    raise ValueError(f"unsupported starting price encoding: {encoding}")


def _wdiv(x: int, y: int) -> int:
    if y <= 0:
        raise ValueError("wdiv denominator must be positive")
    return (x * WAD_INT + y // 2) // y


def _rmul(x: int, y: int) -> int:
    ray = 10**27
    return (x * y + ray // 2) // ray


def _rpow(x: int, n: int) -> int:
    if n < 0:
        raise ValueError("rpow exponent must be non-negative")
    ray = 10**27
    z = x if n % 2 else ray
    n //= 2
    while n:
        x = _rmul(x, x)
        if n % 2:
            z = _rmul(z, x)
        n //= 2
    return z


def latent_terminal_full_lot_ask_raw(
    *,
    encoding: StartingPriceEncoding,
    starting_price_raw: int,
    sell_amount_raw: int,
    sell_decimals: int,
    want_decimals: int,
    step_decay_rate_bps: int,
    step_duration_seconds: int,
    auction_length_seconds: int,
) -> int:
    if starting_price_raw <= 0:
        raise ValueError("starting price must be positive")
    if sell_amount_raw <= 0:
        raise ValueError("sell amount must be positive")
    if not 0 <= sell_decimals <= 18 or not 0 <= want_decimals <= 18:
        raise ValueError("token decimals must be between 0 and 18")
    if not 0 < step_decay_rate_bps < 10_000:
        raise ValueError("step decay rate must be between 1 and 9999 bps")
    if step_duration_seconds <= 0 or auction_length_seconds <= 0:
        raise ValueError("auction timing must be positive")

    sell_scaler = 10 ** (18 - sell_decimals)
    want_scaler = 10 ** (18 - want_decimals)
    available_scaled = sell_amount_raw * sell_scaler
    if encoding == StartingPriceEncoding.WHOLE_WANT:
        initial_price = _wdiv(starting_price_raw * WAD_INT, available_scaled)
    elif encoding == StartingPriceEncoding.WAD_WANT:
        initial_price = _wdiv(starting_price_raw, available_scaled)
    else:
        raise ValueError(f"unsupported starting price encoding: {encoding}")

    ray = 10**27
    final_step = auction_length_seconds // step_duration_seconds
    ray_multiplier = ray - step_decay_rate_bps * 10**23
    terminal_price = _rmul(initial_price, _rpow(ray_multiplier, final_step))
    return (sell_amount_raw * sell_scaler * terminal_price) // WAD_INT // want_scaler


@dataclass(frozen=True)
class AuctionFeasibility:
    starting_price_raw: int
    minimum_price_raw: int
    minimum_quote_raw: int
    minimum_full_lot_raw: int
    terminal_ask_raw: int
    required_quote_raw: int

    @property
    def viable(self) -> bool:
        return self.terminal_ask_raw <= self.minimum_full_lot_raw

    @property
    def clearly_blocked(self) -> bool:
        # A conservative operational cutoff, not a relaxation of the execution guard.
        return self.terminal_ask_raw * 4 >= self.minimum_full_lot_raw * 5


def auction_feasibility(
    *, amount_out_raw: int, floor_quote_raw: int, encoding: StartingPriceEncoding,
    sell_amount_raw: int, sell_decimals: int, want_decimals: int,
    start_buffer_bps: int, min_buffer_bps: int, step_decay_rate_bps: int,
    step_duration_seconds: int, auction_length_seconds: int,
) -> AuctionFeasibility:
    """Use the same integer arithmetic for the execution guard and cheap rechecks."""
    if floor_quote_raw <= 0 or not 0 <= min_buffer_bps < 10_000:
        raise ValueError("positive floor quote and minimum buffer below 10000 bps required")
    starting = encode_starting_price_raw(
        amount_out_raw=amount_out_raw, want_decimals=want_decimals,
        buffer_bps=start_buffer_bps, encoding=encoding,
    )
    minimum = compute_minimum_price_scaled_1e18(
        amount_out_raw=floor_quote_raw, want_decimals=want_decimals,
        sell_amount_raw=sell_amount_raw, sell_decimals=sell_decimals, buffer_bps=min_buffer_bps,
    )
    minimum_quote = compute_minimum_quote_unscaled(
        minimum_price_scaled_1e18=minimum, sell_amount_raw=sell_amount_raw,
        sell_decimals=sell_decimals,
    )
    minimum_full_lot = (
        sell_amount_raw * 10 ** (18 - sell_decimals) * minimum
    ) // WAD_INT // 10 ** (18 - want_decimals)
    terminal = latent_terminal_full_lot_ask_raw(
        encoding=encoding, starting_price_raw=starting, sell_amount_raw=sell_amount_raw,
        sell_decimals=sell_decimals, want_decimals=want_decimals,
        step_decay_rate_bps=step_decay_rate_bps, step_duration_seconds=step_duration_seconds,
        auction_length_seconds=auction_length_seconds,
    )
    # Exact threshold for this starting-price band, including minimum-price rounding.
    sell_scaled = sell_amount_raw * 10 ** (18 - sell_decimals)
    want_scale = 10 ** (18 - want_decimals)
    needed_rate = _ceil_div(terminal * WAD_INT * want_scale, sell_scaled)
    required_quote = _ceil_div(
        needed_rate * sell_amount_raw * 10**want_decimals * 10_000,
        (10_000 - min_buffer_bps) * WAD_INT * 10**sell_decimals,
    )
    return AuctionFeasibility(starting, minimum, minimum_quote, minimum_full_lot, terminal, required_quote)


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
