from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import tidal.transaction_service.planner as planner_module
from tidal.auction_settlement import AuctionLotPreview, AuctionSettlementInspection
from tidal.transaction_service.planner import KickPlanner
from tidal.transaction_service.types import KickCandidate, KickStatus, PreparedKick, TxIntent


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        txn_usd_threshold=100.0,
        txn_data_freshness_limit_seconds=1200,
        txn_max_gas_limit=500000,
        auction_kicker_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        chain_id=1,
        multicall_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        multicall_enabled=True,
        multicall_auction_batch_calls=100,
        kick_config=SimpleNamespace(ignore_policy=object(), cooldown_policy=object()),
    )


def _candidate(*, token_address: str, usd_value: float) -> KickCandidate:
    return KickCandidate(
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        token_address=token_address,
        auction_address="0x3333333333333333333333333333333333333333",
        normalized_balance="1000",
        price_usd="2.5",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=usd_value,
        decimals=18,
        source_name="Test Strategy",
        token_symbol="CRV",
        want_symbol="USDC",
    )


def _prepared(candidate: KickCandidate) -> PreparedKick:
    return PreparedKick(
        candidate=candidate,
        sell_amount=10**21,
        starting_price_unscaled=2750,
        minimum_price_scaled_1e18=2_375_000_000_000_000_000,
        minimum_quote_unscaled=2375,
        sell_amount_str="1000",
        starting_price_unscaled_str="2750",
        minimum_price_scaled_1e18_str="2375000000000000000",
        minimum_quote_unscaled_str="2375",
        usd_value_str=str(int(candidate.usd_value)),
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="2500",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=50,
        step_decay_rate_bps=50,
        pricing_profile_name="stable",
    )


class _FakeKickDeps:
    def __init__(self, *, prepared_by_token: dict[str, PreparedKick]) -> None:
        self.prepared_by_token = prepared_by_token
        self.web3_client = object()
        self.inspect_candidates = AsyncMock(side_effect=self._inspect_candidates)
        self.prepare_kick = AsyncMock(side_effect=self._prepare_kick)

    async def _inspect_candidates(self, candidates: list[KickCandidate]) -> dict[tuple[str, str], None]:
        return {(candidate.auction_address, candidate.token_address): None for candidate in candidates}

    async def _prepare_kick(self, candidate: KickCandidate, run_id: str, inspection=None) -> PreparedKick:
        del run_id, inspection
        return self.prepared_by_token[candidate.token_address]

    def build_single_kick_intent(self, prepared: PreparedKick, *, sender: str | None) -> TxIntent:
        return TxIntent(
            operation="kick",
            to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            data=f"0x1{prepared.candidate.token_address[-1]}",
            value="0x0",
            chain_id=1,
            sender=sender,
        )

    def build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        del prepared_kicks
        return TxIntent(
            operation="kick",
            to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            data="0x9",
            value="0x0",
            chain_id=1,
            sender=sender,
        )

    def build_resolve_auction_intent(self, prepared_operation, *, sender: str | None) -> TxIntent:  # noqa: ANN001
        return TxIntent(
            operation="resolve-auction",
            to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            data=f"0x2{prepared_operation.sell_token[-1]}",
            value="0x0",
            chain_id=1,
            sender=sender,
        )


def _inspection(*previews: AuctionLotPreview) -> AuctionSettlementInspection:
    return AuctionSettlementInspection(
        auction_address="0x3333333333333333333333333333333333333333",
        is_active_auction=any(preview.active for preview in previews),
        enabled_tokens=tuple(preview.token_address for preview in previews),
        requested_token=None,
        lot_previews=previews,
    )


def _preview(
    *,
    token: str,
    path: int,
    active: bool,
    balance_raw: int,
    requires_force: bool = False,
) -> AuctionLotPreview:
    return AuctionLotPreview(
        token_address=token,
        path=path,
        active=active,
        kicked_at=123 if path in {4, 5} else 0,
        balance_raw=balance_raw,
        requires_force=requires_force,
        receiver="0x5555555555555555555555555555555555555555",
        read_ok=True,
    )


@pytest.mark.asyncio
async def test_kick_planner_prepares_resolve_operations_for_dirty_auction(monkeypatch) -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(prepared_by_token={candidate.token_address: _prepared(candidate)})
    inspection = _inspection(_preview(token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", path=5, active=False, balance_raw=10**18))

    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate.auction_address: inspection}),
    )

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        web3_client=object(),
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=AsyncMock(return_value=(210000, 252000, None)),
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=None,
        limit=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-dirty",
        batch=True,
    )

    assert plan.kick_operations == []
    assert len(plan.resolve_operations) == 1
    assert plan.resolve_operations[0].sell_token == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert [intent.operation for intent in plan.tx_intents] == ["resolve-auction"]
    assert plan.skipped_during_prepare[0].reason == "auction requires settlement before kick"
    assert plan.skipped_during_prepare[0].blocked_token_address == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert plan.skipped_during_prepare[0].blocked_reason == "inactive kicked lot with stranded inventory"
    assert plan.skipped_during_prepare[0].next_step == (
        "tidal auction settle 0x3333333333333333333333333333333333333333 "
        "--token 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
    )


@pytest.mark.asyncio
async def test_kick_planner_skips_live_funded_auction_without_force(monkeypatch) -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(prepared_by_token={candidate.token_address: _prepared(candidate)})
    inspection = _inspection(
        _preview(
            token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            path=3,
            active=True,
            balance_raw=10**18,
            requires_force=True,
        )
    )

    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate.auction_address: inspection}),
    )

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        web3_client=object(),
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=AsyncMock(return_value=(210000, 252000, None)),
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=None,
        limit=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-live",
        batch=True,
    )

    assert plan.status() == "noop"
    assert plan.resolve_operations == []
    assert plan.kick_operations == []
    assert plan.skipped_during_prepare[0].reason == "auction still active with live sell balance"
    assert plan.skipped_during_prepare[0].next_step == (
        "tidal auction settle 0x3333333333333333333333333333333333333333 "
        "--token 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa --force"
    )


@pytest.mark.asyncio
async def test_kick_planner_treats_inactive_kicked_empty_lot_as_non_blocking(monkeypatch) -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(prepared_by_token={candidate.token_address: _prepared(candidate)})
    inspection = _inspection(
        _preview(
            token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            path=4,
            active=False,
            balance_raw=0,
        )
    )

    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate.auction_address: inspection}),
    )

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        web3_client=object(),
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=AsyncMock(return_value=(210000, 252000, None)),
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=None,
        limit=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-nonblocking",
        batch=True,
    )

    assert plan.resolve_operations == []
    assert len(plan.kick_operations) == 1
    assert [intent.operation for intent in plan.tx_intents] == ["kick"]


@pytest.mark.asyncio
async def test_kick_planner_points_to_manual_sweep_when_resolve_estimate_hits_amount_zero(monkeypatch) -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(prepared_by_token={candidate.token_address: _prepared(candidate)})
    inspection = _inspection(
        _preview(
            token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            path=5,
            active=False,
            balance_raw=10**18,
        )
    )

    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate.auction_address: inspection}),
    )

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        web3_client=object(),
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=AsyncMock(
            return_value=(None, None, "Gas estimate failed: call to 0x3333…3333 failed: Amount is zero.")
        ),
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=None,
        limit=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-manual-sweep",
        batch=True,
    )

    assert plan.tx_intents == []
    assert plan.resolve_operations == []
    assert plan.skipped_during_prepare[0].reason == "auction requires manual sweep before kick"
    assert plan.skipped_during_prepare[0].next_step == (
        "tidal auction sweep 0x3333333333333333333333333333333333333333 "
        "--token 0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa"
    )
    assert any("This token may require a manual sweep." in warning for warning in plan.warnings)


@pytest.mark.asyncio
async def test_kick_planner_can_preview_without_sender_or_gas_estimation(monkeypatch) -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(prepared_by_token={candidate.token_address: _prepared(candidate)})
    clean_inspection = _inspection()
    estimate_transaction_fn = AsyncMock(return_value=(210000, 252000, None))

    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate.auction_address: clean_inspection}),
    )

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=estimate_transaction_fn,
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=None,
        limit=1,
        sender=None,
        run_id="run-dry",
        batch=True,
        estimate_transactions=False,
    )

    assert len(plan.kick_operations) == 1
    assert len(plan.tx_intents) == 1
    assert plan.tx_intents[0].sender is None
    assert plan.warnings == []
    estimate_transaction_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_kick_planner_falls_back_from_batch_to_individual_intents(monkeypatch) -> None:
    candidate_a = _candidate(token_address="0x1111111111111111111111111111111111111111", usd_value=3000.0)
    candidate_b = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2000.0)
    prepared_a = _prepared(candidate_a)
    prepared_b = _prepared(candidate_b)
    shortlist = SimpleNamespace(
        selected_candidates=[candidate_b, candidate_a],
        eligible_candidates=[candidate_a, candidate_b],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(
        prepared_by_token={
            candidate_a.token_address: prepared_a,
            candidate_b.token_address: prepared_b,
        },
    )
    clean_inspection = _inspection()
    monkeypatch.setattr(
        planner_module,
        "inspect_auction_settlements",
        AsyncMock(return_value={candidate_a.auction_address: clean_inspection}),
    )

    estimate_calls: list[str] = []

    async def estimate_transaction_fn(web3_client, settings, *, sender, to_address, data, gas_cap):  # noqa: ANN001
        del web3_client, settings, sender, to_address, gas_cap
        estimate_calls.append(data)
        if data == "0x9":
            return None, None, "Gas estimate failed: call to 0x3333…3333 failed: active auction"
        if data == "0x11":
            return 210000, 252000, None
        assert data == "0x12"
        return None, None, "Gas estimate failed: call to 0x4444…4444 failed: not enabled"

    planner = KickPlanner(
        session=object(),
        settings=_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: sorted(candidates, key=lambda candidate: candidate.usd_value, reverse=True),
        estimate_transaction_fn=estimate_transaction_fn,
    )

    plan = await planner.plan(
        source_type="strategy",
        source_address=candidate_a.source_address,
        auction_address=candidate_a.auction_address,
        token_address=None,
        limit=2,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-batch",
        batch=True,
    )

    assert estimate_calls == ["0x9", "0x11", "0x12"]
    assert [prepared.candidate.token_address for prepared in plan.kick_operations] == [candidate_a.token_address]
    assert [intent.data for intent in plan.tx_intents] == ["0x11"]
    assert plan.warnings == ["Gas estimate failed: call to 0x4444…4444 failed: not enabled"]
    assert plan.skipped_during_prepare[0].result is not None
    assert plan.skipped_during_prepare[0].result.status == KickStatus.ESTIMATE_FAILED
