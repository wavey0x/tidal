"""Behavioral boundaries for full-balance kicks around a nominal USD limit."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from web3 import Web3

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.pricing.token_price_agg import QuoteResult
from tidal.transaction_service.kick_policy import TokenSizingPolicy
from tidal.transaction_service.kick_prepare import KickPreparer
from tidal.transaction_service.kick_shared import _select_sell_size
from tidal.transaction_service.kick_tx import KickTxBuilder
from tidal.transaction_service.types import AuctionInspection, KickCandidate, KickStatus, PreparedKick


TOKEN = "0x2222222222222222222222222222222222222222"


def _candidate(*, decimals=18, price="1", source_type="strategy"):
    return KickCandidate(
        source_type=source_type,
        source_address="0x1111111111111111111111111111111111111111",
        token_address=TOKEN,
        auction_address="0x3333333333333333333333333333333333333333",
        want_address="0x4444444444444444444444444444444444444444",
        normalized_balance="5000",  # Sizing must use the live balance instead.
        price_usd=price,
        usd_value=5000,
        decimals=decimals,
        auction_version="1.0.5",
    )


@pytest.mark.parametrize("decimals", [0, 6, 18])
@pytest.mark.parametrize(
    ("balance", "expected"),
    [(90, 90), (100, 100), (107, 107), (110, 110), (111, 100), (250, 100)],
)
def test_buffer_sweeps_through_inclusive_boundary(decimals, balance, expected):
    size = _select_sell_size(
        token_sizing_policy=TokenSizingPolicy(Decimal("100"), {}, 1000),
        candidate=_candidate(decimals=decimals),
        live_balance_raw=balance * 10**decimals,
    )
    assert size.selected_sell_raw == expected * 10**decimals
    assert size.selected_sell_usd_value == Decimal(expected)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_boundary_compares_usd_before_rounding_token_units(offset):
    # $3,300 at $2.50 per token: distinguish even one raw unit at 18 decimals.
    balance = 1320 * 10**18 + offset
    size = _select_sell_size(
        token_sizing_policy=TokenSizingPolicy(Decimal("3000"), {}, 1000),
        candidate=_candidate(price="2.5"),
        live_balance_raw=balance,
    )
    assert size.selected_sell_raw == (balance if offset <= 0 else 1200 * 10**18)


@pytest.mark.parametrize(("buffer_bps", "expected"), [(0, 100), (500, 100), (1000, 107), (2500, 107)])
def test_buffer_is_configurable(buffer_bps, expected):
    size = _select_sell_size(
        token_sizing_policy=TokenSizingPolicy(Decimal("100"), {}, buffer_bps),
        candidate=_candidate(decimals=6),
        live_balance_raw=107_000_000,
    )
    assert size.selected_sell_raw == expected * 10**6


@pytest.mark.parametrize(("balance", "expected"), [(220, 220), (221, 200)])
def test_buffer_uses_token_override(balance, expected):
    size = _select_sell_size(
        token_sizing_policy=TokenSizingPolicy(Decimal("100"), {TOKEN: Decimal("200")}, 1000),
        candidate=_candidate(decimals=0),
        live_balance_raw=balance,
    )
    assert size.selected_sell_raw == expected


@pytest.mark.parametrize("policy", [None, TokenSizingPolicy(None, {}, 1000)])
def test_unlimited_token_uses_full_balance(policy):
    size = _select_sell_size(
        token_sizing_policy=policy, candidate=_candidate(decimals=6), live_balance_raw=987_654_321,
    )
    assert size.selected_sell_raw == 987_654_321


def test_capped_amount_rounds_down_to_token_units():
    size = _select_sell_size(
        token_sizing_policy=TokenSizingPolicy(Decimal("100"), {}, 1000),
        candidate=_candidate(decimals=6, price="3"),
        live_balance_raw=40_000_000,
    )
    assert size.selected_sell_raw == 33_333_333
    assert size.selected_sell_usd_value == Decimal("99.999999")


@pytest.mark.parametrize("source_type", ["strategy", "fee_burner"])
@pytest.mark.parametrize(("balance", "expected"), [(3210, 3210), (3300, 3300), (3301, 3000)])
async def test_selected_amount_reaches_quote_and_transaction(source_type, balance, expected):
    candidate = _candidate(source_type=source_type)
    raw_expected = expected * 10**18
    provider = SimpleNamespace(
        quote=AsyncMock(return_value=QuoteResult(
            amount_out_raw=raw_expected,
            token_out_decimals=18,
            provider_statuses={"curve": "ok"},
            provider_amounts={"curve": raw_expected},
        )),
        quote_usd=AsyncMock(return_value=SimpleNamespace(price_usd="1")),
    )
    preparer = KickPreparer(
        web3_client=object(), price_provider=provider, usd_threshold=50,
        token_sizing_policy=TokenSizingPolicy(Decimal("3000"), {}, 1000),
        erc20_reader=SimpleNamespace(
            read_balance=AsyncMock(return_value=balance * 10**18),
            read_decimals=AsyncMock(return_value=18),
        ),
        start_price_buffer_bps=1000, min_price_buffer_bps=500,
    )
    prepared = await preparer.prepare_kick(candidate, "buffer-test", inspection=AuctionInspection(
        auction_address=candidate.auction_address, is_active_auction=False,
        active_tokens=(), auction_version="1.0.5", auction_length_seconds=86_400,
        step_duration_seconds=60,
    ))
    assert isinstance(prepared, PreparedKick)
    assert prepared.sell_amount == raw_expected
    assert prepared.normalized_balance == str(expected)
    provider.quote.assert_awaited_once_with(
        token_in=TOKEN, token_out=candidate.want_address, amount_in=str(raw_expected),
    )

    # Encode and decode real calldata without RPC, checking both execution paths.
    web3 = Web3()
    contract = web3.eth.contract(abi=AUCTION_KICKER_ABI)
    builder = KickTxBuilder(
        web3_client=SimpleNamespace(contract=lambda *_: contract),
        auction_kicker_address="0x9999999999999999999999999999999999999999", chain_id=1,
    )
    single = builder.build_single_kick_intent(prepared, sender=candidate.source_address)
    _, args = contract.decode_function_input(single.data)
    assert list(args.values())[3] == raw_expected
    batch = builder.build_batch_kick_intent([prepared], sender=candidate.source_address)
    _, args = contract.decode_function_input(batch.data)
    assert list(list(args.values())[0][0].values())[3] == raw_expected


@pytest.mark.parametrize(
    ("balance", "reason"),
    [(43, "below threshold on live balance"), (60, "below threshold after token sizing cap")],
)
async def test_buffer_does_not_bypass_minimum_value(balance, reason):
    candidate = _candidate()
    provider = SimpleNamespace(quote=AsyncMock())
    preparer = KickPreparer(
        web3_client=object(), price_provider=provider, usd_threshold=50,
        token_sizing_policy=TokenSizingPolicy(Decimal("40"), {}, 1000),
        erc20_reader=SimpleNamespace(
            read_balance=AsyncMock(return_value=balance * 10**18),
            read_decimals=AsyncMock(return_value=18),
        ),
        start_price_buffer_bps=1000, min_price_buffer_bps=500,
    )
    result = await preparer.prepare_kick(candidate, "buffer-test", inspection=AuctionInspection(
        auction_address=candidate.auction_address, is_active_auction=False,
        active_tokens=(), auction_version="1.0.5", auction_length_seconds=86_400,
        step_duration_seconds=60,
    ))
    assert result.status == KickStatus.SKIP
    assert result.error_message == reason
    provider.quote.assert_not_awaited()
