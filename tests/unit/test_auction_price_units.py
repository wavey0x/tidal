from decimal import Decimal

import pytest

from tidal.auction_price_units import (
    decode_starting_price_amount,
    encode_starting_price_raw,
    latent_terminal_full_lot_ask_raw,
)
from tidal.auction_versions import StartingPriceEncoding


def test_round_8_quote_encodes_by_auction_version() -> None:
    inputs = {
        "amount_out_raw": 96_953_501_367_832_632,
        "want_decimals": 18,
        "buffer_bps": 1_000,
    }

    assert (
        encode_starting_price_raw(
            **inputs,
            encoding=StartingPriceEncoding.WHOLE_WANT,
        )
        == 1
    )
    assert (
        encode_starting_price_raw(
            **inputs,
            encoding=StartingPriceEncoding.WAD_WANT,
        )
        == 106_648_851_504_615_896
    )
    assert decode_starting_price_amount(1, StartingPriceEncoding.WHOLE_WANT) == Decimal(1)
    assert decode_starting_price_amount(
        106_648_851_504_615_896,
        StartingPriceEncoding.WAD_WANT,
    ) == Decimal("0.106648851504615896")


def test_encoder_handles_non_18_decimal_want_tokens() -> None:
    inputs = {"amount_out_raw": 1_234_567, "want_decimals": 6, "buffer_bps": 1_000}

    assert (
        encode_starting_price_raw(
            **inputs,
            encoding=StartingPriceEncoding.WHOLE_WANT,
        )
        == 2
    )
    assert (
        encode_starting_price_raw(
            **inputs,
            encoding=StartingPriceEncoding.WAD_WANT,
        )
        == 1_358_023_700_000_000_000
    )


@pytest.mark.parametrize(
    ("amount_out_raw", "want_decimals", "buffer_bps"),
    [(0, 18, 0), (1, -1, 0), (1, 19, 0), (1, 18, -1)],
)
def test_encoder_rejects_invalid_inputs(
    amount_out_raw: int,
    want_decimals: int,
    buffer_bps: int,
) -> None:
    with pytest.raises(ValueError):
        encode_starting_price_raw(
            amount_out_raw=amount_out_raw,
            want_decimals=want_decimals,
            buffer_bps=buffer_bps,
            encoding=StartingPriceEncoding.WAD_WANT,
        )


def test_v105_encoder_enforces_contract_minimum() -> None:
    with pytest.raises(ValueError, match="greater than 1e9"):
        encode_starting_price_raw(
            amount_out_raw=1,
            want_decimals=18,
            buffer_bps=0,
            encoding=StartingPriceEncoding.WAD_WANT,
        )


def test_round_8_terminal_ask_vectors_are_contract_exact() -> None:
    inputs = {
        "sell_amount_raw": 843_453_068_587_196_135_157,
        "sell_decimals": 18,
        "want_decimals": 18,
        "step_decay_rate_bps": 15,
        "step_duration_seconds": 60,
        "auction_length_seconds": 86_400,
    }

    assert (
        latent_terminal_full_lot_ask_raw(
            **inputs,
            encoding=StartingPriceEncoding.WHOLE_WANT,
            starting_price_raw=1,
        )
        == 115_138_258_855_697_109
    )
    assert (
        latent_terminal_full_lot_ask_raw(
            **inputs,
            encoding=StartingPriceEncoding.WAD_WANT,
            starting_price_raw=106_648_851_504_615_896,
        )
        == 12_279_363_071_201_321
    )
