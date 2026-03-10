from decimal import Decimal

import pytest

from tidal.constants import CVX_ADDRESS, CVX_PRICE_ALIAS_ADDRESS, CVX_WRAPPER_ALIAS_ADDRESS
from tidal.pricing.service import PriceToken, TokenPriceRefreshService
from tidal.pricing.token_logo import TokenLogoValidationResult
from tidal.pricing.token_price_agg import TokenPriceQuote
from tidal.types import TokenLogoState


class FakePriceProvider:
    source_name = "token_price_agg_usd_price"

    def __init__(self, prices: dict[str, Decimal | None], logo_urls: dict[str, str | None] | None = None):
        self.prices = prices
        self.logo_urls = logo_urls or {}
        self.calls: list[tuple[str, int]] = []

    async def quote_usd(self, token_address: str, token_decimals: int) -> TokenPriceQuote:
        self.calls.append((token_address, token_decimals))
        return TokenPriceQuote(
            price_usd=self.prices[token_address],
            quote_amount_in_raw=1,
            logo_url=self.logo_urls.get(token_address),
        )


class FakeLogoValidator:
    source_name = "token_price_agg_logo_url"

    def __init__(self, default_result: TokenLogoValidationResult):
        self.default_result = default_result
        self.calls: list[str | None] = []

    async def validate(self, logo_url: str | None) -> TokenLogoValidationResult:
        self.calls.append(logo_url)
        if self.default_result.logo_url is None:
            return TokenLogoValidationResult(
                logo_url=None,
                status=self.default_result.status,
                error_message=self.default_result.error_message,
            )
        return TokenLogoValidationResult(
            logo_url=logo_url,
            status=self.default_result.status,
            error_message=self.default_result.error_message,
        )


class FakeTokenRepository:
    def __init__(self, logo_states: dict[str, TokenLogoState] | None = None) -> None:
        self.updates: list[dict[str, str | None]] = []
        self.logo_updates: list[dict[str, str | None]] = []
        self.logo_states = logo_states or {}

    def set_latest_price(
        self,
        *,
        address: str,
        price_usd: str | None,
        source: str,
        status: str,
        fetched_at: str,
        run_id: str,
        error_message: str | None,
    ) -> None:
        self.updates.append(
            {
                "address": address,
                "price_usd": price_usd,
                "source": source,
                "status": status,
                "fetched_at": fetched_at,
                "run_id": run_id,
                "error_message": error_message,
            }
        )

    def get_logo_state(self, address: str) -> TokenLogoState | None:
        return self.logo_states.get(address)

    def set_logo_validation(
        self,
        *,
        address: str,
        logo_url: str | None,
        source: str | None,
        status: str,
        validated_at: str,
        error_message: str | None,
    ) -> None:
        self.logo_updates.append(
            {
                "address": address,
                "logo_url": logo_url,
                "source": source,
                "status": status,
                "validated_at": validated_at,
                "error_message": error_message,
            }
        )
        self.logo_states[address] = TokenLogoState(
            address=address,
            logo_url=logo_url,
            logo_status=status,
            logo_validated_at=validated_at,
        )


@pytest.mark.asyncio
async def test_price_alias_uses_cvx_quote_for_alias_token_and_persists_logo() -> None:
    repo = FakeTokenRepository()
    provider = FakePriceProvider(
        prices={
            CVX_ADDRESS: Decimal("3.25"),
        },
        logo_urls={
            CVX_ADDRESS: "https://cdn.example/cvx.png",
        },
    )
    logo_validator = FakeLogoValidator(
        TokenLogoValidationResult(
            logo_url="https://cdn.example/cvx.png",
            status="SUCCESS",
            error_message=None,
        )
    )
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=2,
        price_provider=provider,
        logo_validator=logo_validator,
        token_repository=repo,
    )

    stats, errors = await service.refresh_many(
        run_id="run-1",
        tokens=[
            PriceToken(address=CVX_PRICE_ALIAS_ADDRESS, decimals=18),
            PriceToken(address=CVX_WRAPPER_ALIAS_ADDRESS, decimals=18),
            PriceToken(address=CVX_ADDRESS, decimals=18),
        ],
    )

    assert errors == []
    assert stats["tokens_seen"] == 3
    assert stats["tokens_succeeded"] == 3
    assert provider.calls == [(CVX_ADDRESS, 18)]
    assert logo_validator.calls == ["https://cdn.example/cvx.png"]

    updates_by_address = {item["address"]: item for item in repo.updates}
    assert updates_by_address[CVX_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_WRAPPER_ALIAS_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_ADDRESS]["status"] == "SUCCESS"
    assert updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["status"] == "SUCCESS"
    assert updates_by_address[CVX_WRAPPER_ALIAS_ADDRESS]["status"] == "SUCCESS"

    logo_updates_by_address = {item["address"]: item for item in repo.logo_updates}
    assert logo_updates_by_address[CVX_ADDRESS]["logo_url"] == "https://cdn.example/cvx.png"
    assert logo_updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["logo_url"] == "https://cdn.example/cvx.png"
    assert logo_updates_by_address[CVX_WRAPPER_ALIAS_ADDRESS]["logo_url"] == "https://cdn.example/cvx.png"


@pytest.mark.asyncio
async def test_price_refresh_skips_logo_validation_when_logo_already_cached() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    repo = FakeTokenRepository(
        logo_states={
            token_address: TokenLogoState(
                address=token_address,
                logo_url="https://cdn.example/already-cached.png",
                logo_status="SUCCESS",
                logo_validated_at="2026-03-10T00:00:00+00:00",
            )
        }
    )
    provider = FakePriceProvider(
        prices={token_address: Decimal("4.2")},
        logo_urls={token_address: "https://cdn.example/new-logo.png"},
    )
    logo_validator = FakeLogoValidator(
        TokenLogoValidationResult(
            logo_url="https://cdn.example/new-logo.png",
            status="SUCCESS",
            error_message=None,
        )
    )
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=1,
        price_provider=provider,
        logo_validator=logo_validator,
        token_repository=repo,
    )

    stats, errors = await service.refresh_many(
        run_id="run-1",
        tokens=[PriceToken(address=token_address, decimals=18)],
    )

    assert errors == []
    assert stats["tokens_succeeded"] == 1
    assert logo_validator.calls == []
    assert repo.logo_updates == []


@pytest.mark.asyncio
async def test_price_refresh_retries_logo_not_found_after_backoff() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    repo = FakeTokenRepository(
        logo_states={
            token_address: TokenLogoState(
                address=token_address,
                logo_url=None,
                logo_status="NOT_FOUND",
                logo_validated_at="2026-03-01T00:00:00+00:00",
            )
        }
    )
    provider = FakePriceProvider(
        prices={token_address: None},
        logo_urls={token_address: None},
    )
    logo_validator = FakeLogoValidator(
        TokenLogoValidationResult(
            logo_url=None,
            status="NOT_FOUND",
            error_message=None,
        )
    )
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=1,
        price_provider=provider,
        logo_validator=logo_validator,
        token_repository=repo,
    )

    stats, errors = await service.refresh_many(
        run_id="run-1",
        tokens=[PriceToken(address=token_address, decimals=18)],
    )

    assert errors == []
    assert stats["tokens_not_found"] == 1
    assert logo_validator.calls == [None]
    assert repo.logo_updates[0]["status"] == "NOT_FOUND"
