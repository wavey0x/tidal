from types import SimpleNamespace
from unittest.mock import AsyncMock

from tidal.auction_settlement import AuctionLotPreview, AuctionSettlementInspection
from tidal.ops.kick_inspect import inspect_kick_candidates
from tidal.transaction_service.types import KickCandidate


def _candidate(*, auction_address: str = "0x3333333333333333333333333333333333333333") -> KickCandidate:
    return KickCandidate(
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        token_address="0x2222222222222222222222222222222222222222",
        auction_address=auction_address,
        normalized_balance="1000",
        price_usd="2.5",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=2500.0,
        decimals=18,
        source_name="Test Strategy",
        token_symbol="CRV",
        want_symbol="USDC",
    )


def _preview(
    *,
    token: str,
    path: int,
    active: bool,
    balance_raw: int,
    requires_force: bool,
    error_message: str | None = None,
    read_ok: bool = True,
) -> AuctionLotPreview:
    return AuctionLotPreview(
        token_address=token,
        path=path if read_ok else None,
        active=active if read_ok else None,
        kicked_at=123 if read_ok else None,
        balance_raw=balance_raw if read_ok else None,
        requires_force=requires_force if read_ok else None,
        receiver="0x5555555555555555555555555555555555555555" if read_ok else None,
        read_ok=read_ok,
        error_message=error_message,
    )


def _inspection(*previews: AuctionLotPreview) -> AuctionSettlementInspection:
    return AuctionSettlementInspection(
        auction_address="0x3333333333333333333333333333333333333333",
        is_active_auction=any(preview.active for preview in previews if preview.read_ok),
        enabled_tokens=tuple(preview.token_address for preview in previews),
        requested_token=None,
        lot_previews=tuple(previews),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        txn_usd_threshold=100.0,
        txn_max_data_age_seconds=600,
        rpc_url="http://rpc.example",
        kick_config=SimpleNamespace(
            ignore_policy=SimpleNamespace(),
            cooldown_policy=SimpleNamespace(),
        ),
    )


def test_inspect_kick_candidates_marks_dirty_auction_as_resolve_first(monkeypatch) -> None:
    candidate = _candidate()
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_candidates=[],
        limited_candidates=[],
    )
    monkeypatch.setattr("tidal.ops.kick_inspect.build_shortlist", lambda *args, **kwargs: shortlist)
    monkeypatch.setattr("tidal.ops.kick_inspect.build_web3_client", lambda settings: object())
    monkeypatch.setattr(
        "tidal.ops.kick_inspect.inspect_auction_settlements",
        AsyncMock(
            return_value={
                candidate.auction_address: _inspection(
                    _preview(
                        token=candidate.token_address,
                        path=5,
                        active=False,
                        balance_raw=10**18,
                        requires_force=False,
                    )
                )
            }
        ),
    )

    result = inspect_kick_candidates(object(), _settings())

    assert result.ready_count == 0
    assert result.resolve_first_count == 1
    assert result.resolve_first[0].state == "resolve_first"
    assert result.resolve_first[0].detail == "inactive kicked lot with stranded inventory"


def test_inspect_kick_candidates_marks_live_lot_as_blocked_live(monkeypatch) -> None:
    candidate = _candidate()
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_candidates=[],
        limited_candidates=[],
    )
    monkeypatch.setattr("tidal.ops.kick_inspect.build_shortlist", lambda *args, **kwargs: shortlist)
    monkeypatch.setattr("tidal.ops.kick_inspect.build_web3_client", lambda settings: object())
    monkeypatch.setattr(
        "tidal.ops.kick_inspect.inspect_auction_settlements",
        AsyncMock(
            return_value={
                candidate.auction_address: _inspection(
                    _preview(
                        token=candidate.token_address,
                        path=3,
                        active=True,
                        balance_raw=10**18,
                        requires_force=True,
                    )
                )
            }
        ),
    )

    result = inspect_kick_candidates(object(), _settings())

    assert result.ready_count == 0
    assert result.blocked_live_count == 1
    assert result.blocked_live[0].state == "blocked_live"
    assert result.blocked_live[0].detail == "live funded lot"


def test_inspect_kick_candidates_marks_preview_failures(monkeypatch) -> None:
    candidate = _candidate()
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_candidates=[],
        limited_candidates=[],
    )
    monkeypatch.setattr("tidal.ops.kick_inspect.build_shortlist", lambda *args, **kwargs: shortlist)
    monkeypatch.setattr("tidal.ops.kick_inspect.build_web3_client", lambda settings: object())
    monkeypatch.setattr(
        "tidal.ops.kick_inspect.inspect_auction_settlements",
        AsyncMock(
            return_value={
                candidate.auction_address: _inspection(
                    _preview(
                        token=candidate.token_address,
                        path=0,
                        active=False,
                        balance_raw=0,
                        requires_force=False,
                        read_ok=False,
                        error_message="multicall preview failed",
                    )
                )
            }
        ),
    )

    result = inspect_kick_candidates(object(), _settings())

    assert result.preview_failed_count == 1
    assert result.preview_failed[0].detail == "multicall preview failed"
