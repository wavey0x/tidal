from types import SimpleNamespace
from unittest.mock import MagicMock

from tidal.auction_settlement import (
    AuctionSettlementDecision,
    build_auction_settlement_call,
    decide_auction_settlement,
    normalize_settlement_method,
)
from tidal.transaction_service.types import AuctionInspection


def _make_inspection(**overrides) -> AuctionInspection:
    defaults = {
        "auction_address": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "is_active_auction": True,
        "active_tokens": ("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        "active_token": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "active_available_raw": 10**18,
        "active_price_raw": 110,
        "minimum_price_raw": 100,
    }
    defaults.update(overrides)
    return AuctionInspection(**defaults)


def test_normalize_settlement_method_accepts_dash_form() -> None:
    assert normalize_settlement_method("sweep-and-settle") == "sweep_and_settle"


def test_decide_auction_settlement_no_active_lot_is_noop_in_auto_mode() -> None:
    decision = decide_auction_settlement(
        _make_inspection(
            is_active_auction=False,
            active_tokens=(),
            active_token=None,
            active_available_raw=None,
            active_price_raw=None,
            minimum_price_raw=None,
        )
    )

    assert decision.status == "noop"
    assert decision.operation_type is None
    assert decision.reason == "auction has no active lot"


def test_decide_auction_settlement_no_active_lot_is_error_for_forced_method() -> None:
    decision = decide_auction_settlement(
        _make_inspection(
            is_active_auction=False,
            active_tokens=(),
            active_token=None,
            active_available_raw=None,
            active_price_raw=None,
            minimum_price_raw=None,
        ),
        method="settle",
    )

    assert decision.status == "error"
    assert decision.reason == "requested settlement method is not applicable: auction has no active lot"


def test_decide_auction_settlement_sold_out_selects_settle() -> None:
    decision = decide_auction_settlement(_make_inspection(active_available_raw=0, active_price_raw=125))

    assert decision.status == "actionable"
    assert decision.operation_type == "settle"
    assert decision.token_address == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert decision.reason == "active lot is sold out"


def test_decide_auction_settlement_floor_price_selects_sweep_and_settle() -> None:
    decision = decide_auction_settlement(_make_inspection(active_available_raw=10**18, active_price_raw=100))

    assert decision.status == "actionable"
    assert decision.operation_type == "sweep_and_settle"
    assert decision.reason == "active auction price is at or below minimumPrice"


def test_decide_auction_settlement_above_floor_is_noop_in_auto_mode() -> None:
    decision = decide_auction_settlement(_make_inspection(active_available_raw=10**18, active_price_raw=101))

    assert decision.status == "noop"
    assert decision.operation_type is None
    assert decision.reason == "auction still active above minimumPrice"


def test_decide_auction_settlement_above_floor_is_error_for_forced_method() -> None:
    decision = decide_auction_settlement(
        _make_inspection(active_available_raw=10**18, active_price_raw=101),
        method="sweep_and_settle",
    )

    assert decision.status == "error"
    assert decision.reason == "requested settlement method is not applicable: auction still active above minimumPrice"


def test_decide_auction_settlement_token_override_mismatch_errors() -> None:
    decision = decide_auction_settlement(
        _make_inspection(),
        token_address="0xcccccccccccccccccccccccccccccccccccccccc",
    )

    assert decision.status == "error"
    assert decision.token_address == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert "does not match active token" in decision.reason


def test_build_auction_settlement_call_for_settle_targets_auction() -> None:
    mock_contract = MagicMock()
    mock_settle = MagicMock()
    mock_settle._encode_transaction_data.return_value = "0xdeadbeef"
    mock_contract.functions.settle.return_value = mock_settle

    web3_client = MagicMock()
    web3_client.contract.return_value = mock_contract

    call = build_auction_settlement_call(
        settings=SimpleNamespace(auction_kicker_address="0x9999999999999999999999999999999999999999"),
        web3_client=web3_client,
        auction_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        decision=AuctionSettlementDecision(
            status="actionable",
            operation_type="settle",
            token_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            reason="active lot is sold out",
        ),
    )

    assert call.operation_type == "settle"
    assert call.target_address == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert call.data == "0xdeadbeef"


def test_build_auction_settlement_call_for_sweep_targets_kicker() -> None:
    mock_contract = MagicMock()
    mock_sweep = MagicMock()
    mock_sweep._encode_transaction_data.return_value = "0xfeedface"
    mock_contract.functions.sweepAndSettle.return_value = mock_sweep

    web3_client = MagicMock()
    web3_client.contract.return_value = mock_contract

    call = build_auction_settlement_call(
        settings=SimpleNamespace(auction_kicker_address="0x9999999999999999999999999999999999999999"),
        web3_client=web3_client,
        auction_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        decision=AuctionSettlementDecision(
            status="actionable",
            operation_type="sweep_and_settle",
            token_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            reason="active auction price is at or below minimumPrice",
        ),
    )

    assert call.operation_type == "sweep_and_settle"
    assert call.target_address == "0x9999999999999999999999999999999999999999"
    assert call.data == "0xfeedface"
