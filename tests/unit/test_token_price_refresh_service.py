from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from tidal.constants import CVX_ADDRESS, CVX_PRICE_ALIAS_ADDRESS, CVX_WRAPPER_ALIAS_ADDRESS
from tidal.persistence import models
from tidal.persistence.repositories import TokenRepository
from tidal.pricing.service import PriceToken, TokenPriceRefreshService
from tidal.pricing.token_price_agg import TokenPriceQuote


class FakePriceProvider:
    source_name = "token_price_agg_usd_price"

    def __init__(self, prices: dict[str, Decimal | None]):
        self.prices = prices
        self.calls: list[tuple[str, int]] = []

    async def quote_usd(self, token_address: str, token_decimals: int) -> TokenPriceQuote:
        self.calls.append((token_address, token_decimals))
        return TokenPriceQuote(
            price_usd=self.prices[token_address],
            quote_amount_in_raw=1,
        )


class RaisingPriceProvider:
    source_name = "token_price_agg_usd_price"

    def __init__(self, exc: Exception):
        self.exc = exc

    async def quote_usd(self, token_address: str, token_decimals: int) -> TokenPriceQuote:
        del token_address
        del token_decimals
        raise self.exc


class FakeTokenRepository:
    def __init__(self) -> None:
        self.updates: list[dict[str, str | None]] = []
        self.failure_updates: list[dict[str, str | None]] = []

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

    def mark_price_refresh_failed(
        self,
        *,
        address: str,
        source: str,
        status: str,
        fetched_at: str,
        run_id: str,
        error_message: str | None,
    ) -> None:
        self.failure_updates.append(
            {
                "address": address,
                "source": source,
                "status": status,
                "fetched_at": fetched_at,
                "run_id": run_id,
                "error_message": error_message,
            }
        )

def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://prices.example/v1/price?status={status_code}")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def _seed_token(session: Session, *, address: str, price_usd: str | None = None, price_status: str | None = None) -> None:
    session.execute(
        insert(models.tokens).values(
            address=address,
            chain_id=1,
            name="Test Token",
            symbol="TEST",
            decimals=18,
            is_core_reward=0,
            price_usd=price_usd,
            price_source="token_price_agg_usd_price" if price_status is not None else None,
            price_status=price_status,
            price_fetched_at="2026-01-01T00:00:00+00:00" if price_status is not None else None,
            price_run_id="seed" if price_status is not None else None,
            first_seen_at="2026-01-01T00:00:00+00:00",
            last_seen_at="2026-01-01T00:00:00+00:00",
        )
    )


@pytest.mark.asyncio
async def test_price_alias_uses_one_cvx_quote_for_all_alias_tokens() -> None:
    repo = FakeTokenRepository()
    provider = FakePriceProvider(prices={CVX_ADDRESS: Decimal("3.25")})
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=2,
        price_provider=provider,
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

    updates_by_address = {item["address"]: item for item in repo.updates}
    assert updates_by_address[CVX_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_PRICE_ALIAS_ADDRESS]["price_usd"] == "3.25"
    assert updates_by_address[CVX_WRAPPER_ALIAS_ADDRESS]["price_usd"] == "3.25"

@pytest.mark.asyncio
async def test_transient_price_failure_preserves_existing_price() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_token(session, address=token_address, price_usd="1.23", price_status="SUCCESS")
        service = TokenPriceRefreshService(
            chain_id=1,
            enabled=True,
            concurrency=1,
            price_provider=RaisingPriceProvider(_http_error(429)),
            token_repository=TokenRepository(session),
        )

        stats, errors = await service.refresh_many(
            run_id="run-1",
            tokens=[PriceToken(address=token_address, decimals=18)],
        )
        session.commit()

        row = session.execute(select(models.tokens).where(models.tokens.c.address == token_address)).mappings().one()

    assert stats["tokens_failed"] == 1
    assert len(errors) == 1
    assert row["price_usd"] == "1.23"
    assert row["price_status"] == "FAILED"
    assert "429" in row["price_error_message"]


@pytest.mark.asyncio
async def test_transient_price_failure_without_existing_price_keeps_null_price() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_token(session, address=token_address)
        service = TokenPriceRefreshService(
            chain_id=1,
            enabled=True,
            concurrency=1,
            price_provider=RaisingPriceProvider(_http_error(429)),
            token_repository=TokenRepository(session),
        )

        stats, _ = await service.refresh_many(
            run_id="run-1",
            tokens=[PriceToken(address=token_address, decimals=18)],
        )
        session.commit()

        row = session.execute(select(models.tokens).where(models.tokens.c.address == token_address)).mappings().one()

    assert stats["tokens_failed"] == 1
    assert row["price_usd"] is None
    assert row["price_status"] == "FAILED"


@pytest.mark.asyncio
async def test_not_found_price_refresh_clears_existing_price() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_token(session, address=token_address, price_usd="1.23", price_status="SUCCESS")
        service = TokenPriceRefreshService(
            chain_id=1,
            enabled=True,
            concurrency=1,
            price_provider=RaisingPriceProvider(_http_error(404)),
            token_repository=TokenRepository(session),
        )

        stats, errors = await service.refresh_many(
            run_id="run-1",
            tokens=[PriceToken(address=token_address, decimals=18)],
        )
        session.commit()

        row = session.execute(select(models.tokens).where(models.tokens.c.address == token_address)).mappings().one()

    assert errors == []
    assert stats["tokens_not_found"] == 1
    assert row["price_usd"] is None
    assert row["price_status"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_successful_price_refresh_replaces_stale_failure() -> None:
    token_address = "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"
    engine = create_engine("sqlite:///:memory:", future=True)
    models.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_token(session, address=token_address, price_usd="1.23", price_status="FAILED")
        service = TokenPriceRefreshService(
            chain_id=1,
            enabled=True,
            concurrency=1,
            price_provider=FakePriceProvider(prices={token_address: Decimal("4.56")}),
            token_repository=TokenRepository(session),
        )

        stats, errors = await service.refresh_many(
            run_id="run-1",
            tokens=[PriceToken(address=token_address, decimals=18)],
        )
        session.commit()

        row = session.execute(select(models.tokens).where(models.tokens.c.address == token_address)).mappings().one()

    assert errors == []
    assert stats["tokens_succeeded"] == 1
    assert row["price_usd"] == "4.56"
    assert row["price_status"] == "SUCCESS"
    assert row["price_error_message"] is None


@pytest.mark.asyncio
async def test_price_delay_paces_request_starts_globally(monkeypatch) -> None:
    token_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    token_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    token_c = "0xcccccccccccccccccccccccccccccccccccccccc"
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("tidal.pricing.service.asyncio.sleep", fake_sleep)

    repo = FakeTokenRepository()
    provider = FakePriceProvider(
        prices={
            token_a: Decimal("1"),
            token_b: Decimal("2"),
            token_c: Decimal("3"),
        },
    )
    service = TokenPriceRefreshService(
        chain_id=1,
        enabled=True,
        concurrency=3,
        delay_seconds=0.25,
        price_provider=provider,
        token_repository=repo,
    )

    stats, errors = await service.refresh_many(
        run_id="run-1",
        tokens=[
            PriceToken(address=token_a, decimals=18),
            PriceToken(address=token_b, decimals=18),
            PriceToken(address=token_c, decimals=18),
        ],
    )

    assert errors == []
    assert stats["tokens_succeeded"] == 3
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == pytest.approx(0.25, abs=0.001)
    assert sleep_calls[1] == pytest.approx(0.25, abs=0.001)
