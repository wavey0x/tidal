from decimal import Decimal

import pytest

from tidal.pricing.token_price_agg import QuoteResult, TokenPriceAggProvider, TokenPriceNotFoundError


def _provider(api_key: str | None = None) -> TokenPriceAggProvider:
    return TokenPriceAggProvider(
        chain_id=1,
        base_url="https://prices.wavey.info",
        api_key=api_key,
        timeout_seconds=10,
        retry_attempts=1,
    )


def test_extract_price_usd_reads_summary_high_price() -> None:
    provider = _provider()
    payload = {
        "summary": {
            "successful_providers": 3,
            "high_price": "1.2345",
        }
    }

    price = provider._extract_price_usd(payload)  # noqa: SLF001
    assert price == Decimal("1.2345")


def test_extract_price_usd_not_found_when_high_price_missing() -> None:
    provider = _provider()
    payload = {
        "summary": {
            "successful_providers": 0,
            "high_price": None,
        },
        "providers": {
            "curve": {"status": "no_route"},
            "defillama": {"status": "bad_request"},
            "enso": {"status": "error"},
        },
    }

    with pytest.raises(TokenPriceNotFoundError):
        _ = provider._extract_price_usd(payload)  # noqa: SLF001


def test_extract_price_usd_missing_high_price_with_transient_errors() -> None:
    provider = _provider()
    payload = {
        "summary": {
            "successful_providers": 0,
            "high_price": None,
        },
        "providers": {
            "curve": {"status": "error"},
            "defillama": {"status": "error"},
        },
    }

    with pytest.raises(ValueError):
        _ = provider._extract_price_usd(payload)  # noqa: SLF001


@pytest.mark.asyncio
async def test_quote_usd_requests_v1_price_with_token_and_chain_id() -> None:
    provider = _provider()
    captured: dict[str, object] = {}

    async def fake_get_price(client, path, params):  # noqa: ANN001
        captured["base_url"] = str(client.base_url)
        captured["path"] = path
        captured["params"] = params
        captured["authorization"] = client.headers.get("authorization")
        return {
            "token": {
                "logo_url": "https://assets.example/logo.png",
            },
            "summary": {
                "successful_providers": 1,
                "high_price": "4.2",
            }
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    quote = await provider.quote_usd("0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B", 18)

    assert quote.price_usd == Decimal("4.2")
    assert quote.quote_amount_in_raw == 1
    assert quote.logo_url == "https://assets.example/logo.png"
    assert captured["base_url"] == "https://prices.wavey.info"
    assert captured["path"] == "/v1/price"
    assert captured["params"] == {
        "token": "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b",
        "chain_id": 1,
        "use_underlying": "true",
    }
    assert captured["authorization"] is None


@pytest.mark.asyncio
async def test_quote_usd_sends_authorization_bearer_when_configured() -> None:
    provider = _provider(api_key="test-key")
    captured: dict[str, object] = {}

    async def fake_get_price(client, path, params):  # noqa: ANN001
        captured["authorization"] = client.headers.get("authorization")
        return {
            "summary": {
                "successful_providers": 1,
                "high_price": "1",
            }
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    await provider.quote_usd("0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B", 18)

    assert captured["authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_quote_usd_returns_logo_url_when_price_not_found() -> None:
    provider = _provider()

    async def fake_get_price(client, path, params):  # noqa: ANN001
        del client
        del path
        del params
        return {
            "token": {
                "logo_url": "https://assets.example/logo.png",
            },
            "summary": {
                "successful_providers": 0,
                "high_price": None,
            },
            "providers": {
                "curve": {"status": "no_route"},
            },
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    quote = await provider.quote_usd("0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B", 18)

    assert quote.price_usd is None
    assert quote.logo_url == "https://assets.example/logo.png"


@pytest.mark.asyncio
async def test_quote_usd_treats_http_404_payload_without_summary_as_not_found() -> None:
    provider = _provider()

    async def fake_get_price(client, path, params):  # noqa: ANN001
        del client
        del path
        del params
        return {
            "_fd_http_status": 404,
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    quote = await provider.quote_usd("0x4e3FBD56CD56c3e72c1403e103b45Db9da5B9D2B", 18)

    assert quote.price_usd is None
    assert quote.logo_url is None


# ---------------------------------------------------------------------------
# curve_quote_available tests
# ---------------------------------------------------------------------------


def test_curve_quote_available_with_positive_amount() -> None:
    result = QuoteResult(amount_out_raw=100, token_out_decimals=6, provider_amounts={"curve": 742100})
    assert result.curve_quote_available() is True


def test_curve_quote_available_with_zero_amount() -> None:
    result = QuoteResult(amount_out_raw=100, token_out_decimals=6, provider_amounts={"curve": 0})
    assert result.curve_quote_available() is False


def test_curve_quote_available_missing_curve() -> None:
    result = QuoteResult(amount_out_raw=100, token_out_decimals=6, provider_amounts={"defillama": 742100})
    assert result.curve_quote_available() is False


def test_curve_quote_available_empty_amounts() -> None:
    result = QuoteResult(amount_out_raw=100, token_out_decimals=6)
    assert result.curve_quote_available() is False


@pytest.mark.asyncio
async def test_quote_parses_per_provider_amounts() -> None:
    provider = _provider()
    captured: dict[str, object] = {}

    async def fake_get_price(client, path, params):  # noqa: ANN001
        del client
        captured["path"] = path
        captured["params"] = params
        return {
            "summary": {"high_amount_out": "742100"},
            "token_out": {"decimals": 6},
            "providers": {
                "curve": {"status": "ok", "amount_out": 742100},
                "defillama": {"status": "ok", "amount_out": 740000},
                "enso": {"status": "error", "amount_out": None},
            },
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    result = await provider.quote(
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "1000000000000000000",
    )

    assert result.provider_amounts == {"curve": 742100, "defillama": 740000}
    assert result.curve_quote_available() is True
    assert captured["path"] == "/v1/quote"
    assert captured["params"] == {
        "token_in": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "token_out": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_in": "1000000000000000000",
        "chain_id": 1,
        "use_underlying": "true",
        "timeout_ms": 7000,
    }


# ---------------------------------------------------------------------------
# quote soft-retry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_retries_on_all_provider_failure_then_succeeds() -> None:
    """First call returns all-error (amount_out_raw=None), retry succeeds."""
    provider = _provider()
    call_count = 0

    async def fake_get_price(client, path, params):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "summary": {"high_amount_out": None},
                "token_out": {"decimals": 6},
                "providers": {
                    "curve": {"status": "error", "amount_out": None},
                    "enso": {"status": "error", "amount_out": None},
                },
            }
        return {
            "summary": {"high_amount_out": "742100"},
            "token_out": {"decimals": 6},
            "providers": {
                "curve": {"status": "ok", "amount_out": 742100},
                "enso": {"status": "ok", "amount_out": 740000},
            },
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    result = await provider.quote(
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "1000000000000000000",
    )

    assert call_count == 2
    assert result.amount_out_raw == 742100


@pytest.mark.asyncio
async def test_quote_returns_none_when_both_attempts_fail() -> None:
    """Both attempts return all-error — no infinite retry."""
    provider = _provider()
    call_count = 0

    async def fake_get_price(client, path, params):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return {
            "summary": {"high_amount_out": None},
            "token_out": {"decimals": 6},
            "providers": {
                "curve": {"status": "error", "amount_out": None},
                "enso": {"status": "no_route", "amount_out": None},
            },
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    result = await provider.quote(
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "1000000000000000000",
    )

    assert call_count == 2
    assert result.amount_out_raw is None


@pytest.mark.asyncio
async def test_quote_no_retry_when_no_providers() -> None:
    """No providers in response — don't retry (not a rate-limit issue)."""
    provider = _provider()
    call_count = 0

    async def fake_get_price(client, path, params):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return {
            "summary": {"high_amount_out": None},
            "token_out": {"decimals": 6},
        }

    provider._get_price = fake_get_price  # type: ignore[method-assign]  # noqa: SLF001
    result = await provider.quote(
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "1000000000000000000",
    )

    assert call_count == 1
    assert result.amount_out_raw is None
