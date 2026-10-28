"""Recorded rounding failure, bounded quote use, and conservative recovery."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.auction_price_units import auction_feasibility
from tidal.auction_versions import StartingPriceEncoding
from tidal.persistence import models
from tidal.pricing.token_price_agg import QuoteResult
from tidal.transaction_service.feasibility import KickFeasibilityHolds
from tidal.transaction_service.kick_policy import PricingPolicy, PricingProfile
from tidal.transaction_service.kick_prepare import KickPreparer
from tidal.transaction_service.types import (
    AuctionInspection,
    KickCandidate,
    KickResult,
    KickSkipReason,
    KickStatus,
    PreparedKick,
)

NOW = datetime.now(timezone.utc)
SELL = 1228541993821503888021
QUOTE = 87849952477382483
CANDIDATE = KickCandidate(
    source_type="strategy",
    source_address="0x" + "11" * 20,
    token_address="0x" + "22" * 20,
    auction_address="0x" + "33" * 20,
    want_address="0x" + "44" * 20,
    normalized_balance="1228.541993821503888021",
    price_usd="1",
    usd_value=1228.54,
    decimals=18,
    auction_version="1.0.4",
    token_symbol="CRV",
    want_symbol="YFIETH-f",
)
CONTEXT = dict(
    chainId=1,
    want=CANDIDATE.want_address,
    version="1.0.4",
    encoding="whole_want",
    sellDecimals=18,
    wantDecimals=18,
    startBufferBps=1000,
    minBufferBps=500,
    decayBps=15,
    stepSeconds=60,
    lengthSeconds=86400,
    outlierFloor=True,
    sizeCapUsd="None",
    requireCurve=True,
)
# Match the public enum rather than rely on its serialized spelling.
CONTEXT["encoding"] = StartingPriceEncoding.WHOLE_WANT.value


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'holds.db'}")
    models.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def holds(session):
    return KickFeasibilityHolds(session, chain_id=1, price_max_age_seconds=1200)


def block(controller, *, now=NOW, floor=QUOTE, context=None, sell=SELL):
    context = context or CONTEXT
    return controller.blocked(
        CANDIDATE,
        context=context,
        assessment=controller.assess(
            context, sell_amount=sell, high=floor, floor=floor
        ),
        high=floor,
        floor=floor,
        sell_amount=sell,
        live_balance=SELL,
        now=now,
    )


def prices(session, ratio="1", *, now=NOW, status="SUCCESS"):
    for address, price in (
        (CANDIDATE.token_address, ratio),
        (CANDIDATE.want_address, "1"),
    ):
        values = dict(
            chain_id=1,
            decimals=18,
            price_usd=price,
            price_status=status,
            price_fetched_at=now.isoformat(),
            first_seen_at=now.isoformat(),
            last_seen_at=now.isoformat(),
        )
        existing = session.execute(
            select(models.tokens).where(models.tokens.c.address == address)
        ).first()
        if existing:
            session.execute(
                models.tokens.update()
                .where(models.tokens.c.address == address)
                .values(**values)
            )
        else:
            session.execute(models.tokens.insert().values(address=address, **values))
    session.commit()


def test_recorded_yfi_failure_and_precise_version(session):
    assessment = holds(session).assess(
        CONTEXT, sell_amount=SELL, high=QUOTE, floor=QUOTE
    )
    assert assessment.terminal_ask_raw == 115138258855697365
    assert assessment.clearly_blocked and not assessment.viable
    assert 121198000000000000 < assessment.required_quote_raw < 121199000000000000
    precise = CONTEXT | dict(
        version="1.0.5", encoding=StartingPriceEncoding.WAD_WANT.value
    )
    assert (
        holds(session).assess(precise, sell_amount=SELL, high=QUOTE, floor=QUOTE).viable
    )


@pytest.mark.parametrize(
    "sell_decimals,want_decimals", [(18, 18), (6, 18), (18, 6), (6, 6)]
)
def test_reported_recovery_threshold_respects_integer_rounding(
    sell_decimals, want_decimals
):
    inputs = dict(
        amount_out_raw=87849 * 10**want_decimals // 1000000,
        floor_quote_raw=87849 * 10**want_decimals // 1000000,
        encoding=StartingPriceEncoding.WHOLE_WANT,
        sell_amount_raw=1228 * 10**sell_decimals,
        sell_decimals=sell_decimals,
        want_decimals=want_decimals,
        start_buffer_bps=1000,
        min_buffer_bps=500,
        step_decay_rate_bps=15,
        step_duration_seconds=60,
        auction_length_seconds=86400,
    )
    result = auction_feasibility(**inputs)
    assert auction_feasibility(
        **(
            inputs
            | dict(
                amount_out_raw=result.required_quote_raw,
                floor_quote_raw=result.required_quote_raw,
            )
        )
    ).viable
    assert not auction_feasibility(
        **(
            inputs
            | dict(
                amount_out_raw=result.required_quote_raw - 1,
                floor_quote_raw=result.required_quote_raw - 1,
            )
        )
    ).viable


def test_daily_deadline_and_restart_preserve_hold(session):
    first = block(holds(session))
    session.commit()
    with Session(session.bind) as restarted:
        controller = holds(restarted)
        for minute in range(1, 100):
            deferred = controller.deferred(
                CANDIDATE,
                context=CONTEXT,
                sell_amount=SELL,
                live_balance=SELL + minute,
                now=NOW + timedelta(minutes=minute),
            )
            assert deferred["nextFullCheckAt"] == first["nextFullCheckAt"]
        assert (
            controller.deferred(
                CANDIDATE,
                context=CONTEXT,
                sell_amount=SELL,
                now=NOW + timedelta(days=1),
            )
            is None
        )


def test_selected_lot_and_fresh_market_can_wake_but_dust_and_stale_data_cannot(session):
    controller = holds(session)
    prices(session)
    first = block(controller)
    assert controller.deferred(
        CANDIDATE, context=CONTEXT, sell_amount=SELL + 1, now=NOW + timedelta(seconds=1)
    )
    # An uncapped balance increase does not help if the selected lot stays capped.
    assert (
        controller.deferred(
            CANDIDATE,
            context=CONTEXT,
            sell_amount=SELL,
            live_balance=SELL * 2,
            now=NOW + timedelta(seconds=2),
        )["nextFullCheckAt"]
        == first["nextFullCheckAt"]
    )
    assert (
        controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL * 2, now=NOW)
        is None
    )
    prices(session, "1.2")
    assert (
        controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL, now=NOW)
        is None
    )
    prices(session, "2", now=NOW - timedelta(hours=1))
    assert controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL, now=NOW)
    prices(session, "2", status="FAILED")
    assert controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL, now=NOW)


@pytest.mark.parametrize(
    "change",
    [
        dict(decayBps=16),
        dict(lengthSeconds=172800),
        dict(sizeCapUsd="500"),
        dict(outlierFloor=False),
        dict(version="1.0.5"),
        dict(chainId=42161),
        dict(want="0x" + "55" * 20),
    ],
)
def test_relevant_context_changes_wake(session, change):
    controller = holds(session)
    block(controller)
    assert (
        controller.deferred(
            CANDIDATE, context=CONTEXT | change, sell_amount=SELL, now=NOW
        )
        is None
    )


def test_newly_available_prices_and_corrupt_rows_get_fresh_evaluation(session):
    controller = holds(session)
    block(controller)
    prices(session)
    assert (
        controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL, now=NOW)
        is None
    )
    session.execute(models.kick_prepare_status_latest.update().values(detail_json="[]"))
    assert (
        controller.deferred(CANDIDATE, context=CONTEXT, sell_amount=SELL, now=NOW)
        is None
    )


def preparer(session, *, amount=QUOTE, curve=True, version="1.0.4"):
    candidate = replace(CANDIDATE, auction_version=version)
    provider = SimpleNamespace(
        quote=AsyncMock(
            return_value=QuoteResult(
                amount_out_raw=amount,
                token_out_decimals=18,
                provider_statuses={"curve": "ok" if curve else "error"},
                provider_amounts={"curve": amount} if curve and amount else {},
            )
        ),
        quote_usd=AsyncMock(return_value=SimpleNamespace(price_usd="10000")),
    )
    p = KickPreparer(
        web3_client=object(),
        price_provider=provider,
        usd_threshold=100,
        require_curve_quote=True,
        erc20_reader=SimpleNamespace(
            read_balance=AsyncMock(return_value=SELL),
            read_decimals=AsyncMock(return_value=18),
        ),
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
        feasibility_holds=holds(session),
        pricing_policy=PricingPolicy(
            default_profile_name="volatile",
            profiles={
                "volatile": PricingProfile(
                    name="volatile",
                    start_price_buffer_bps=1000,
                    min_price_buffer_bps=500,
                    step_decay_rate_bps=15,
                    outlier_floor_enabled=True,
                )
            },
            profile_overrides={},
        ),
    )
    inspection = AuctionInspection(
        auction_address=candidate.auction_address,
        is_active_auction=False,
        active_tokens=(),
        auction_version=version,
        auction_length_seconds=86400,
        step_duration_seconds=60,
    )
    return p, provider, candidate, inspection


@pytest.mark.asyncio
async def test_repeated_preparation_skips_provider_and_refresh_keeps_guard(session):
    p, provider, candidate, inspection = preparer(session)
    first = await p.prepare_kick(candidate, "first", inspection=inspection)
    assert isinstance(first, KickResult) and first.status == KickStatus.SKIP
    assert first.reason_data["nextFullCheckAt"]
    for i in range(100):
        result = await p.prepare_kick(candidate, str(i), inspection=inspection)
        assert result.reason_code == KickSkipReason.AUCTION_FEASIBILITY_HOLD
    assert provider.quote.await_count == 1
    refreshed = await p.prepare_kick(
        candidate, "refresh", inspection=inspection, refresh_quote=True
    )
    assert isinstance(refreshed, KickResult) and refreshed.status == KickStatus.SKIP
    assert provider.quote.await_count == 2
    provider.quote.return_value = QuoteResult(
        amount_out_raw=13 * 10**16,
        token_out_decimals=18,
        provider_statuses={"curve": "ok"},
        provider_amounts={"curve": 13 * 10**16},
    )
    assert isinstance(
        await p.prepare_kick(
            candidate, "recovered", inspection=inspection, refresh_quote=True
        ),
        PreparedKick,
    )
    assert (
        session.execute(select(models.kick_prepare_status_latest.c.status)).scalar_one()
        == "RECOVERED"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount,curve", [(None, False), (QUOTE, False), (10**17, True)]
)
async def test_errors_and_borderline_prices_do_not_hold(session, amount, curve):
    p, provider, candidate, inspection = preparer(session, amount=amount, curve=curve)
    for i in range(2):
        assert isinstance(
            await p.prepare_kick(candidate, str(i), inspection=inspection), KickResult
        )
    assert provider.quote.await_count == 2
    assert session.execute(select(models.kick_prepare_status_latest)).first() is None


@pytest.mark.asyncio
async def test_v105_small_quote_remains_viable(session):
    p, provider, candidate, inspection = preparer(session, version="1.0.5")
    assert isinstance(
        await p.prepare_kick(candidate, "precise", inspection=inspection), PreparedKick
    )
    assert session.execute(select(models.kick_prepare_status_latest)).first() is None
