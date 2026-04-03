from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.transaction_service.planner import KickPlanner
from tidal.transaction_service.types import KickCandidate, KickRecoveryPlan, KickStatus, PreparedKick, TxIntent


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        txn_usd_threshold=100.0,
        txn_max_data_age_seconds=600,
        txn_max_gas_limit=500000,
        auction_kicker_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        chain_id=1,
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
    def __init__(
        self,
        *,
        prepared_by_token: dict[str, PreparedKick],
        recovered_by_token: dict[str, PreparedKick] | None = None,
    ) -> None:
        self.prepared_by_token = prepared_by_token
        self.recovered_by_token = recovered_by_token or {}
        self.web3_client = object()
        self.inspect_candidates = AsyncMock(side_effect=self._inspect_candidates)
        self.prepare_kick = AsyncMock(side_effect=self._prepare_kick)
        self.plan_recovery = AsyncMock(side_effect=self._plan_recovery)

    async def _inspect_candidates(self, candidates: list[KickCandidate]) -> dict[tuple[str, str], None]:
        return {(candidate.auction_address, candidate.token_address): None for candidate in candidates}

    async def _prepare_kick(self, candidate: KickCandidate, run_id: str, inspection=None) -> PreparedKick:
        del run_id, inspection
        return self.prepared_by_token[candidate.token_address]

    async def _plan_recovery(self, prepared: PreparedKick) -> PreparedKick | None:
        return self.recovered_by_token.get(prepared.candidate.token_address)

    def build_single_kick_intent(self, prepared: PreparedKick, *, sender: str | None) -> TxIntent:
        prefix = "0x2" if prepared.recovery_plan is not None else "0x1"
        return TxIntent(
            operation="kick",
            to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            data=f"{prefix}{prepared.candidate.token_address[-1]}",
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


@pytest.mark.asyncio
async def test_kick_planner_recovers_single_candidate_after_active_auction_estimate_failure() -> None:
    candidate = _candidate(token_address="0x2222222222222222222222222222222222222222", usd_value=2500.0)
    prepared = _prepared(candidate)
    recovered = replace(
        prepared,
        recovery_plan=KickRecoveryPlan(
            settle_after_start=("0x5555555555555555555555555555555555555555",),
        ),
    )
    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(
        prepared_by_token={candidate.token_address: prepared},
        recovered_by_token={candidate.token_address: recovered},
    )

    async def estimate_transaction_fn(web3_client, settings, *, sender, to_address, data, gas_cap):  # noqa: ANN001
        del web3_client, settings, sender, to_address, gas_cap
        if data == "0x12":
            return None, None, "Gas estimate failed: call to 0x3333…3333 failed: active auction"
        assert data == "0x22"
        return 210000, 252000, None

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
        token_address=candidate.token_address,
        limit=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        run_id="run-1",
        batch=True,
    )

    assert plan.warnings == []
    assert plan.skipped_during_prepare == []
    assert plan.status() == "ok"
    assert plan.kick_operations == [recovered]
    assert [intent.data for intent in plan.tx_intents] == ["0x22"]
    assert plan.tx_intents[0].gas_estimate == 210000
    assert plan.tx_intents[0].gas_limit == 252000
    assert deps.plan_recovery.await_count == 1
    assert plan.to_preview_payload()["preparedOperations"][0]["recoveryPlan"] == {
        "settleAfterStart": ["0x5555555555555555555555555555555555555555"],
        "settleAfterMin": [],
        "settleAfterDecay": [],
    }


@pytest.mark.asyncio
async def test_kick_planner_falls_back_from_batch_to_individual_intents() -> None:
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
        run_id="run-2",
        batch=True,
    )

    assert estimate_calls == ["0x9", "0x11", "0x12"]
    assert [candidate.token_address for candidate in plan.ranked_candidates] == [
        candidate_a.token_address,
        candidate_b.token_address,
    ]
    assert [prepared.candidate.token_address for prepared in plan.kick_operations] == [candidate_a.token_address]
    assert [intent.data for intent in plan.tx_intents] == ["0x11"]
    assert plan.warnings == ["Gas estimate failed: call to 0x4444…4444 failed: not enabled"]
    assert [skip.candidate.token_address for skip in plan.skipped_during_prepare] == [candidate_b.token_address]
    assert plan.skipped_during_prepare[0].reason == "Gas estimate failed: call to 0x4444…4444 failed: not enabled"
    assert plan.skipped_during_prepare[0].result is not None
    assert plan.skipped_during_prepare[0].result.status == KickStatus.ESTIMATE_FAILED
