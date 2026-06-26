"""Focused unit tests for kick preparation and resolver execution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository
from tidal.pricing.token_price_agg import QuoteResult
from tidal.transaction_service.kick_execute import KickExecutor
from tidal.transaction_service.kick_policy import PricingPolicy, PricingProfile
from tidal.transaction_service.kick_prepare import KickPreparer
from tidal.transaction_service.kick_tx import KickTxBuilder
from tidal.transaction_service.types import AuctionInspection, KickCandidate, KickStatus, PreparedKick, PreparedResolveAuction


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _candidate() -> KickCandidate:
    return KickCandidate(
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        token_address="0x2222222222222222222222222222222222222222",
        auction_address="0x3333333333333333333333333333333333333333",
        normalized_balance="1000",
        price_usd="2.5",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=2500.0,
        decimals=18,
        source_name="Test Strategy",
        token_symbol="CRV",
        want_symbol="USDC",
    )


def _pricing_policy(profile: PricingProfile) -> PricingPolicy:
    return PricingPolicy(
        default_profile_name=profile.name,
        profiles={profile.name: profile},
        profile_overrides={},
    )


async def _prepare_with_quote(profile: PricingProfile, quote_result: QuoteResult) -> PreparedKick:
    candidate = _candidate()
    preparer = KickPreparer(
        web3_client=object(),
        price_provider=SimpleNamespace(
            quote=AsyncMock(return_value=quote_result),
            quote_usd=AsyncMock(return_value=SimpleNamespace(price_usd="1")),
        ),
        usd_threshold=100.0,
        require_curve_quote=True,
        erc20_reader=SimpleNamespace(read_balance=AsyncMock(return_value=1000 * 10**18)),
        pricing_policy=_pricing_policy(profile),
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
    )
    result = await preparer.prepare_kick(
        candidate,
        "run-1",
        inspection=AuctionInspection(
            auction_address=candidate.auction_address,
            is_active_auction=False,
            active_tokens=(),
        ),
    )
    assert isinstance(result, PreparedKick)
    return result


def _quote_result(*, high: int, provider_amounts: dict[str, int]) -> QuoteResult:
    return QuoteResult(
        amount_out_raw=high * 10**18,
        token_out_decimals=18,
        provider_statuses={provider: "ok" for provider in provider_amounts},
        provider_amounts={provider: amount * 10**18 for provider, amount in provider_amounts.items()},
    )


class _FakeSigner:
    address = "0xcccccccccccccccccccccccccccccccccccccccc"
    checksum_address = "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"

    def sign_transaction(self, tx):  # noqa: ARG002
        return b"signed"


@pytest.mark.asyncio
async def test_kick_preparer_uses_non_high_provider_median_for_stable_outlier_floor() -> None:
    profile = PricingProfile(
        name="stable",
        start_price_buffer_bps=100,
        min_price_buffer_bps=250,
        step_decay_rate_bps=2,
        outlier_floor_enabled=True,
    )
    quote = _quote_result(
        high=1200,
        provider_amounts={
            "bad": 1200,
            "curve": 1000,
            "defillama": 1002,
            "enso": 998,
        },
    )

    prepared = await _prepare_with_quote(profile, quote)

    assert prepared.starting_price_unscaled == 1212
    assert prepared.minimum_quote_unscaled == 975
    assert prepared.minimum_price_scaled_1e18 == 975_000_000_000_000_000
    assert prepared.quote_amount_str == "1200"


@pytest.mark.asyncio
async def test_kick_preparer_keeps_high_floor_when_stable_quote_has_nearby_provider() -> None:
    profile = PricingProfile(
        name="stable",
        start_price_buffer_bps=100,
        min_price_buffer_bps=250,
        step_decay_rate_bps=2,
        outlier_floor_enabled=True,
    )
    quote = _quote_result(
        high=1200,
        provider_amounts={
            "bad": 1200,
            "curve": 1180,
            "defillama": 1002,
            "enso": 998,
        },
    )

    prepared = await _prepare_with_quote(profile, quote)

    assert prepared.minimum_quote_unscaled == 1170
    assert prepared.minimum_price_scaled_1e18 == 1_170_000_000_000_000_000


@pytest.mark.asyncio
async def test_kick_preparer_keeps_high_floor_when_stable_outlier_lacks_non_high_depth() -> None:
    profile = PricingProfile(
        name="stable",
        start_price_buffer_bps=100,
        min_price_buffer_bps=250,
        step_decay_rate_bps=2,
        outlier_floor_enabled=True,
    )
    quote = _quote_result(
        high=1200,
        provider_amounts={
            "bad": 1200,
            "curve": 1000,
        },
    )

    prepared = await _prepare_with_quote(profile, quote)

    assert prepared.minimum_quote_unscaled == 1170
    assert prepared.minimum_price_scaled_1e18 == 1_170_000_000_000_000_000


@pytest.mark.asyncio
async def test_kick_preparer_keeps_high_floor_when_outlier_floor_disabled() -> None:
    profile = PricingProfile(
        name="volatile",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=250,
        step_decay_rate_bps=15,
        outlier_floor_enabled=False,
    )
    quote = _quote_result(
        high=1200,
        provider_amounts={
            "bad": 1200,
            "curve": 1000,
            "defillama": 1002,
            "enso": 998,
        },
    )

    prepared = await _prepare_with_quote(profile, quote)

    assert prepared.minimum_quote_unscaled == 1170
    assert prepared.minimum_price_scaled_1e18 == 1_170_000_000_000_000_000


@pytest.mark.asyncio
async def test_kick_preparer_skips_active_auction() -> None:
    candidate = _candidate()
    preparer = KickPreparer(
        web3_client=object(),
        price_provider=MagicMock(),
        usd_threshold=100.0,
        require_curve_quote=True,
        erc20_reader=MagicMock(),
        auction_state_reader=SimpleNamespace(
            read_bool_noargs_many=AsyncMock(return_value={candidate.auction_address: True}),
        ),
        start_price_buffer_bps=1000,
        min_price_buffer_bps=50,
    )

    result = await preparer.prepare_kick(candidate, "run-1")

    assert result.status == KickStatus.SKIP
    assert result.error_message == "auction still active"


def test_kick_tx_builder_encodes_resolve_auction_with_force_flag() -> None:
    mock_contract = MagicMock()
    mock_resolve = MagicMock()
    mock_resolve._encode_transaction_data.return_value = "0xfeedface"
    mock_contract.functions.resolveAuction.return_value = mock_resolve

    web3_client = MagicMock()
    web3_client.contract.return_value = mock_contract

    builder = KickTxBuilder(
        web3_client=web3_client,
        auction_kicker_address="0x9999999999999999999999999999999999999999",
        chain_id=1,
    )
    prepared = PreparedResolveAuction(
        candidate=_candidate(),
        sell_token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        path=3,
        reason="live funded lot",
        balance_raw=10**18,
        requires_force=True,
        receiver="0x5555555555555555555555555555555555555555",
    )

    intent = builder.build_resolve_auction_intent(prepared, sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    assert intent.operation == "resolve-auction"
    assert intent.data == "0xfeedface"
    mock_contract.functions.resolveAuction.assert_called_once()
    args = mock_contract.functions.resolveAuction.call_args.args
    assert args[2] is True


@pytest.mark.asyncio
async def test_kick_executor_execute_resolve_auction_persists_confirmed_row(session) -> None:
    candidate = _candidate()
    prepared = PreparedResolveAuction(
        candidate=candidate,
        sell_token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        path=5,
        reason="inactive kicked lot with stranded inventory",
        balance_raw=10**18,
        requires_force=False,
        receiver="0x5555555555555555555555555555555555555555",
        token_symbol="CRV",
        normalized_balance="1000",
    )

    tx_builder = MagicMock()
    tx_builder.build_resolve_auction_intent.return_value = SimpleNamespace(
        operation="resolve-auction",
        to="0x9999999999999999999999999999999999999999",
        data="0xfeedface",
    )
    web3_client = SimpleNamespace(
        get_base_fee=AsyncMock(return_value=int(0.1 * 1e9)),
        estimate_gas=AsyncMock(return_value=100_000),
        get_max_priority_fee=AsyncMock(return_value=int(1 * 1e9)),
        get_transaction_count=AsyncMock(return_value=7),
        send_raw_transaction=AsyncMock(return_value="0xresolvehash"),
        get_transaction_receipt=AsyncMock(
            return_value={
                "status": 1,
                "gasUsed": 123456,
                "effectiveGasPrice": 300000000,
                "blockNumber": 999,
            }
        ),
    )
    executor = KickExecutor(
        web3_client=web3_client,
        signer=_FakeSigner(),
        kick_tx_repository=KickTxRepository(session),
        tx_builder=tx_builder,
        base_fee_cap_gwei=1.0,
        max_priority_fee_gwei=2,
        max_gas_limit=500000,
        chain_id=1,
    )

    result = await executor.execute_resolve_auction(prepared, "run-1")

    assert result.status == KickStatus.CONFIRMED
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["operation_type"] == "resolve_auction"
    assert rows[0]["status"] == "CONFIRMED"
    assert rows[0]["token_address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert rows[0]["stuck_abort_reason"] == "inactive kicked lot with stranded inventory"
