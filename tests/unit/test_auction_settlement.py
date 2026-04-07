from types import SimpleNamespace
from unittest.mock import MagicMock

from tidal.auction_settlement import (
    AuctionLotPreview,
    AuctionSettlementDecision,
    AuctionSettlementInspection,
    AuctionSettlementOperation,
    build_auction_settlement_calls,
    decide_auction_settlement,
)


def _inspection(*previews: AuctionLotPreview, requested_token: str | None = None) -> AuctionSettlementInspection:
    return AuctionSettlementInspection(
        auction_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        is_active_auction=True,
        enabled_tokens=tuple(preview.token_address for preview in previews),
        requested_token=requested_token,
        lot_previews=previews,
    )


def _preview(
    *,
    token: str = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    path: int = 0,
    active: bool = False,
    kicked_at: int = 0,
    balance_raw: int = 0,
    requires_force: bool = False,
    read_ok: bool = True,
    error_message: str | None = None,
) -> AuctionLotPreview:
    return AuctionLotPreview(
        token_address=token,
        path=path if read_ok else None,
        active=active if read_ok else None,
        kicked_at=kicked_at if read_ok else None,
        balance_raw=balance_raw if read_ok else None,
        requires_force=requires_force if read_ok else None,
        receiver="0xcccccccccccccccccccccccccccccccccccccccc" if read_ok else None,
        read_ok=read_ok,
        error_message=error_message,
    )


def test_decide_auction_settlement_noops_on_live_funded_lot_by_default() -> None:
    decision = decide_auction_settlement(
        _inspection(_preview(path=3, active=True, balance_raw=10**18, requires_force=True))
    )

    assert decision.status == "noop"
    assert decision.operations == ()
    assert decision.reason == "auction is progressing normally"


def test_decide_auction_settlement_requires_force_for_targeted_live_lot() -> None:
    token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    decision = decide_auction_settlement(
        _inspection(_preview(token=token, path=3, active=True, balance_raw=10**18, requires_force=True)),
        token_address=token,
        force=True,
    )

    assert decision.status == "actionable"
    assert len(decision.operations) == 1
    assert decision.operations[0].token_address == token
    assert decision.operations[0].requires_force is True
    assert decision.reason == "live funded lot"


def test_decide_auction_settlement_prepares_all_default_actionable_lots() -> None:
    first = _preview(path=1, active=True, balance_raw=0)
    second = _preview(
        token="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        path=5,
        active=False,
        kicked_at=123,
        balance_raw=99,
    )
    decision = decide_auction_settlement(_inspection(first, second))

    assert decision.status == "actionable"
    assert [operation.path for operation in decision.operations] == [1, 5]
    assert decision.reason == "prepared 2 resolvable lot(s)"


def test_decide_auction_settlement_errors_on_requested_token_mismatch() -> None:
    requested = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    resolved = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    decision = decide_auction_settlement(
        _inspection(
            _preview(token=requested, path=0, active=False, balance_raw=0),
            _preview(token=resolved, path=5, active=False, kicked_at=123, balance_raw=99),
        ),
        token_address=requested,
    )

    assert decision.status == "error"
    assert decision.operations == ()
    assert decision.reason == (
        "requested token 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa does not match "
        "resolved token 0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB"
    )


def test_decide_auction_settlement_errors_on_failed_auction_wide_preview() -> None:
    decision = decide_auction_settlement(
        _inspection(
            _preview(path=1, active=True, balance_raw=0),
            _preview(
                token="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                read_ok=False,
                error_message="multicall preview failed",
            ),
        )
    )

    assert decision.status == "error"
    assert decision.reason == "one or more enabled lot previews failed; retry or pass --token"


def test_build_auction_settlement_calls_targets_resolver_with_force_flag() -> None:
    mock_contract = MagicMock()
    mock_resolve = MagicMock()
    mock_resolve._encode_transaction_data.return_value = "0xfeedface"
    mock_contract.functions.resolveAuction.return_value = mock_resolve

    web3_client = MagicMock()
    web3_client.contract.return_value = mock_contract

    decision = AuctionSettlementDecision(
        status="actionable",
        operations=(
            AuctionSettlementOperation(
                operation_type="resolve_auction",
                token_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                path=3,
                reason="live funded lot",
                balance_raw=10**18,
                requires_force=True,
                receiver="0xcccccccccccccccccccccccccccccccccccccccc",
            ),
        ),
        reason="live funded lot",
    )

    calls = build_auction_settlement_calls(
        settings=SimpleNamespace(auction_kicker_address="0x9999999999999999999999999999999999999999"),
        web3_client=web3_client,
        auction_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        decision=decision,
    )

    assert len(calls) == 1
    assert calls[0].force_live is True
    assert calls[0].data == "0xfeedface"
    mock_contract.functions.resolveAuction.assert_called_once()
