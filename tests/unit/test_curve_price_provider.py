from decimal import Decimal

import pytest

from tidal.pricing.curve import CurvePriceProvider, _chain_slug


def _provider() -> CurvePriceProvider:
    return CurvePriceProvider(
        chain_id=1,
        base_url="https://prices.curve.finance",
        timeout_seconds=10,
        retry_attempts=1,
    )


def test_extract_price_usd_prefers_top_level_price() -> None:
    provider = _provider()
    payload = {
        "price": "1.2345",
        "data": [{"usd_price": "1.1111"}],
    }

    price = provider._extract_price_usd(payload)  # noqa: SLF001
    assert price == Decimal("1.2345")


def test_extract_price_usd_reads_nested_usd_price() -> None:
    provider = _provider()
    payload = {
        "data": [
            {
                "usd_price": "2.5",
            }
        ]
    }

    price = provider._extract_price_usd(payload)  # noqa: SLF001
    assert price == Decimal("2.5")


def test_extract_price_usd_raises_on_unknown_shape() -> None:
    provider = _provider()
    payload = {"data": [{"foo": "bar"}]}

    with pytest.raises(ValueError):
        _ = provider._extract_price_usd(payload)  # noqa: SLF001


def test_extract_price_usd_accepts_list_payload_shape() -> None:
    provider = _provider()
    payload = [{"price": "4.2"}]

    price = provider._extract_price_usd(payload)  # noqa: SLF001
    assert price == Decimal("4.2")


def test_extract_price_usd_handles_nested_usd_field() -> None:
    provider = _provider()
    payload = {"data": {"coin": {"usd": "0.57"}}}

    price = provider._extract_price_usd(payload)  # noqa: SLF001
    assert price == Decimal("0.57")


def test_chain_slug_ethereum() -> None:
    assert _chain_slug(1) == "ethereum"


def test_chain_slug_unsupported() -> None:
    with pytest.raises(ValueError):
        _ = _chain_slug(10)
