from decimal import Decimal

import pytest

from factory_dashboard.pricing.token_price_agg import TokenPriceAggProvider, TokenPriceNotFoundError


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
            "curve": {"status": "unsupported_token"},
            "defillama": {"status": "invalid_request"},
            "enso": {"status": "timeout"},
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
            "curve": {"status": "timeout"},
            "defillama": {"status": "timeout"},
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
                "curve": {"status": "unsupported_token"},
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
