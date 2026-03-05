from decimal import Decimal

import pytest

from tidal.constants import CVX_ADDRESS, CVX_PRICE_ALIAS_ADDRESS
from tidal.pricing.curve import CurveQuote
from tidal.pricing.service import PriceToken, TokenPriceRefreshService


class FakeCurveProvider:
    source_name = "curve_usd_price"

    def __init__(self, prices: dict[str, Decimal]):
        self.prices = prices
        self.calls: list[tuple[str, int]] = []

    async def quote_usd(self, token_address: str, token_decimals: int) -> CurveQuote:
        self.calls.append((token_address, token_decimals))
        return CurveQuote(price_usd=self.prices[token_address], quote_amount_in_raw=1)


class FakeTokenRepository:
    def __init__(self) -> None:
        self.updates: list[dict[str, str | None]] = []

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


@pytest.mark.asyncio
async def test_price_alias_uses_cvx_quote_for_alias_token() -> None:
    repo = FakeTokenRepository()
    provider = FakeCurveProvider(
        prices={
            CVX_ADDRESS: Decimal("3.25"),
        }
    )
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=2,
        curve_provider=provider,
        token_repository=repo,
    )

    stats, errors = await service.refresh_many(
        run_id="run-1",
        tokens=[
            PriceToken(address=CVX_PRICE_ALIAS_ADDRESS, decimals=18),
            PriceToken(address=CVX_ADDRESS, decimals=18),
        ],
    )

    assert errors == []
    assert stats["tokens_seen"] == 2
    assert stats["tokens_succeeded"] == 2
    assert provider.calls == [(CVX_ADDRESS, 18)]

    updates_by_address = {item["address"]: item for item in repo.updates}
    assert updates_by_address[CVX_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_ADDRESS]["status"] == "SUCCESS"
    assert updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["status"] == "SUCCESS"
