from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.api.services.action_prepare import (
    _estimate_transaction,
    load_strategy_deploy_defaults,
    prepare_deploy_browser_action,
    prepare_enable_tokens_action,
    prepare_kick_action,
)
from tidal.ops.auction_enable import AuctionInspection, SourceResolution, TokenDiscovery, TokenProbe
from tidal.transaction_service.planner import KickPlanner
from tidal.transaction_service.types import KickCandidate, KickRecoveryPlan, PreparedKick, TxIntent


class _FailingWeb3Client:
    async def estimate_gas(self, tx):  # noqa: ANN001
        del tx
        payload = (
            "0xef3dcb2f"
            "0000000000000000000000000000000000000000000000000000000000000020"
            "0000000000000000000000009cd1fe813c8a7e74f9c6c2c2d2cc63afd634b187"
            "0000000000000000000000000000000000000000000000000000000000000060"
            "000000000000000000000000000000000000000000000000000000000000000b"
            "6e6f7420656e61626c6564000000000000000000000000000000000000000000"
        )
        raise RuntimeError((payload, payload))


def _kick_settings() -> SimpleNamespace:
    return SimpleNamespace(
        txn_usd_threshold=100.0,
        txn_max_data_age_seconds=600,
        kick_config=SimpleNamespace(ignore_policy=object(), cooldown_policy=object()),
        txn_max_gas_limit=500000,
        auction_kicker_address="0x5555555555555555555555555555555555555555",
        chain_id=1,
    )


class _FakeKickDeps:
    def __init__(
        self,
        *,
        candidate: KickCandidate,
        prepared: PreparedKick,
        recovered: PreparedKick | None = None,
        single_data: str = "0xdeadbeef",
        batch_data: str = "0xdeadbeef",
        extended_data: str = "0xfeedface",
    ) -> None:
        inspection_key = (candidate.auction_address, candidate.token_address)
        self.inspect_candidates = AsyncMock(return_value={inspection_key: None})
        self.prepare_kick = AsyncMock(return_value=prepared)
        self.plan_recovery = AsyncMock(return_value=recovered)
        self.single_data = single_data
        self.batch_data = batch_data
        self.extended_data = extended_data

    def build_single_kick_intent(self, prepared_kick: PreparedKick, *, sender: str | None) -> TxIntent:
        data = self.extended_data if prepared_kick.recovery_plan is not None else self.single_data
        return TxIntent(
            operation="kick",
            to="0x5555555555555555555555555555555555555555",
            data=data,
            value="0x0",
            chain_id=1,
            sender=sender,
        )

    def build_batch_kick_intent(self, prepared_kicks: list[PreparedKick], *, sender: str | None) -> TxIntent:
        del prepared_kicks
        return TxIntent(
            operation="kick",
            to="0x5555555555555555555555555555555555555555",
            data=self.batch_data,
            value="0x0",
            chain_id=1,
            sender=sender,
        )


def _build_kick_planner(
    *,
    shortlist,
    deps: _FakeKickDeps,
    estimate_transaction_fn,
) -> KickPlanner:
    return KickPlanner(
        session=object(),
        settings=_kick_settings(),
        preparer=deps,
        tx_builder=deps,
        kick_tx_repository=object(),  # type: ignore[arg-type]
        shortlist_builder=lambda *args, **kwargs: shortlist,
        candidate_sorter=lambda candidates: list(candidates),
        estimate_transaction_fn=estimate_transaction_fn,
    )


@pytest.mark.asyncio
async def test_estimate_transaction_decodes_execution_failed_reverts() -> None:
    gas_estimate, gas_limit, gas_warning = await _estimate_transaction(
        _FailingWeb3Client(),
        SimpleNamespace(chain_id=1),
        sender="0x1111111111111111111111111111111111111111",
        to_address="0x2222222222222222222222222222222222222222",
        data="0xdeadbeef",
        gas_cap=500000,
    )

    assert gas_estimate is None
    assert gas_limit is None
    assert gas_warning == "Gas estimate failed: call to 0x9cd1…b187 failed: not enabled"


@pytest.mark.asyncio
async def test_prepare_kick_action_threads_curve_quote_override(monkeypatch) -> None:
    candidate = KickCandidate(
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
    prepared = PreparedKick(
        candidate=candidate,
        sell_amount=10**21,
        starting_price_unscaled=2750,
        minimum_price_scaled_1e18=2_375_000_000_000_000_000,
        minimum_quote_unscaled=2375,
        sell_amount_str="1000",
        starting_price_unscaled_str="2750",
        minimum_price_scaled_1e18_str="2375000000000000000",
        minimum_quote_unscaled_str="2375",
        usd_value_str="2500",
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="2500",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=50,
        step_decay_rate_bps=50,
        pricing_profile_name="stable",
        settle_token=None,
    )

    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(candidate=candidate, prepared=prepared)

    captured: dict[str, object] = {}

    def fake_build_txn_service(settings, session, **kwargs):  # noqa: ANN001, ANN003
        del settings, session
        captured["require_curve_quote"] = kwargs.get("require_curve_quote")
        return SimpleNamespace(
            planner=_build_kick_planner(
                shortlist=shortlist,
                deps=deps,
                estimate_transaction_fn=AsyncMock(return_value=(210000, 252000, None)),
            )
        )

    monkeypatch.setattr("tidal.api.services.action_prepare.build_txn_service", fake_build_txn_service)
    monkeypatch.setattr("tidal.api.services.action_prepare.create_prepared_action", lambda *args, **kwargs: "action-1")

    status, warnings, data = await prepare_kick_action(
        session=object(),
        settings=_kick_settings(),
        operator_id="tester",
        source_type="strategy",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=candidate.token_address,
        limit=1,
        sender="0x6666666666666666666666666666666666666666",
        require_curve_quote=False,
    )

    assert captured["require_curve_quote"] is False
    assert status == "ok"
    assert warnings == []
    assert data["actionId"] == "action-1"
    preview_item = data["preview"]["preparedOperations"][0]
    assert preview_item["startingPriceDisplay"] == "2,750 USDC (+10.00% buffer)"
    assert preview_item["minimumQuoteDisplay"] == "2,375 USDC (-0.50% buffer)"


@pytest.mark.asyncio
async def test_prepare_kick_action_skips_unsendable_batch_kick_when_gas_estimate_fails(monkeypatch) -> None:
    candidate = KickCandidate(
        source_type="fee_burner",
        source_address="0x1111111111111111111111111111111111111111",
        token_address="0x2222222222222222222222222222222222222222",
        auction_address="0x3333333333333333333333333333333333333333",
        normalized_balance="1000",
        price_usd="1.0",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=1000.0,
        decimals=18,
        source_name="Fee Burner",
        token_symbol="CRV",
        want_symbol="crvUSD",
    )
    prepared = PreparedKick(
        candidate=candidate,
        sell_amount=10**21,
        starting_price_unscaled=1100,
        minimum_price_scaled_1e18=950_000_000_000_000_000,
        minimum_quote_unscaled=950,
        sell_amount_str="1000",
        starting_price_unscaled_str="1100",
        minimum_price_scaled_1e18_str="950000000000000000",
        minimum_quote_unscaled_str="950",
        usd_value_str="1000",
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="1000",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
        step_decay_rate_bps=25,
        pricing_profile_name="volatile",
        settle_token=None,
    )

    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        ignored_skips=[],
        cooldown_skips=[],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    deps = _FakeKickDeps(candidate=candidate, prepared=prepared)
    estimate_mock = AsyncMock(
        return_value=(None, None, "Gas estimate failed: call to 0x3333…3333 failed: active auction")
    )

    monkeypatch.setattr(
        "tidal.api.services.action_prepare.build_txn_service",
        lambda settings, session, **kwargs: SimpleNamespace(
            planner=_build_kick_planner(
                shortlist=shortlist,
                deps=deps,
                estimate_transaction_fn=estimate_mock,
            )
        ),
    )
    create_prepared_action = AsyncMock()
    monkeypatch.setattr("tidal.api.services.action_prepare.create_prepared_action", create_prepared_action)

    status, warnings, data = await prepare_kick_action(
        session=object(),
        settings=_kick_settings(),
        operator_id="tester",
        source_type="fee_burner",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=candidate.token_address,
        limit=1,
        sender="0x6666666666666666666666666666666666666666",
        require_curve_quote=None,
    )

    assert status == "noop"
    assert warnings == ["Gas estimate failed: call to 0x3333…3333 failed: active auction"]
    assert data["transactions"] == []
    assert data["preview"]["preparedOperations"] == []
    assert data["preview"]["skippedDuringPrepare"] == [
        {
            "sourceAddress": candidate.source_address,
            "sourceName": candidate.source_name,
            "auctionAddress": candidate.auction_address,
            "tokenAddress": candidate.token_address,
            "tokenSymbol": candidate.token_symbol,
            "wantSymbol": candidate.want_symbol,
            "reason": "Gas estimate failed: call to 0x3333…3333 failed: active auction",
        }
    ]
    create_prepared_action.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_enable_tokens_action_targets_auction_kicker(monkeypatch) -> None:
    inspection = AuctionInspection(
        auction_address="0x1111111111111111111111111111111111111111",
        governance="0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b",
        want="0x2222222222222222222222222222222222222222",
        receiver="0x3333333333333333333333333333333333333333",
        version="1.0.0",
        in_configured_factory=True,
        governance_matches_required=True,
        enabled_tokens=(),
    )
    source = SourceResolution(
        source_type="strategy",
        source_address="0x3333333333333333333333333333333333333333",
        source_name="Test Strategy",
        warnings=(),
    )
    eligible_probe = TokenProbe(
        token_address="0x4444444444444444444444444444444444444444",
        origins=("manual",),
        symbol="CRV",
        decimals=18,
        raw_balance=1,
        normalized_balance="1",
        status="eligible",
        reason="eligible",
    )
    execution_plan = SimpleNamespace(
        to_address="0x5555555555555555555555555555555555555555",
        data="0xdeadbeef",
        call_succeeded=True,
        gas_estimate=210000,
        error_message=None,
        sender_authorized=True,
        authorization_target="0x5555555555555555555555555555555555555555",
    )
    enabler = SimpleNamespace(
        inspect_auction=lambda auction: inspection,
        resolve_source=lambda inspection: source,
        discover_tokens=lambda **kwargs: TokenDiscovery(tokens_by_address={eligible_probe.token_address: {"manual"}}, notes=[]),
        probe_tokens=lambda **kwargs: [eligible_probe],
        build_execution_plan=lambda **kwargs: execution_plan,
    )

    monkeypatch.setattr("tidal.api.services.action_prepare.build_sync_web3", lambda settings: object())
    monkeypatch.setattr("tidal.api.services.action_prepare.AuctionTokenEnabler", lambda w3, settings: enabler)
    monkeypatch.setattr("tidal.api.services.action_prepare.create_prepared_action", lambda *args, **kwargs: "action-enable")

    status, warnings, data = await prepare_enable_tokens_action(
        settings=SimpleNamespace(
            chain_id=1,
            txn_max_gas_limit=500000,
        ),
        session=object(),
        operator_id="tester",
        auction_address=inspection.auction_address,
        sender="0x6666666666666666666666666666666666666666",
        extra_tokens=[],
    )

    assert status == "ok"
    assert warnings == []
    assert data["transactions"][0]["to"] == execution_plan.to_address
    assert data["transactions"][0]["data"] == execution_plan.data
    assert data["preview"]["executionTarget"] == execution_plan.to_address
    assert data["preview"]["previewSenderAuthorized"] is True


@pytest.mark.asyncio
async def test_prepare_enable_tokens_action_returns_error_on_governance_mismatch(monkeypatch) -> None:
    inspection = AuctionInspection(
        auction_address="0x1111111111111111111111111111111111111111",
        governance="0x9999999999999999999999999999999999999999",
        want="0x2222222222222222222222222222222222222222",
        receiver="0x3333333333333333333333333333333333333333",
        version="1.0.0",
        in_configured_factory=True,
        governance_matches_required=False,
        enabled_tokens=(),
    )
    source = SourceResolution(
        source_type="strategy",
        source_address="0x3333333333333333333333333333333333333333",
        source_name="Test Strategy",
        warnings=(),
    )
    eligible_probe = TokenProbe(
        token_address="0x4444444444444444444444444444444444444444",
        origins=("manual",),
        symbol="CRV",
        decimals=18,
        raw_balance=1,
        normalized_balance="1",
        status="eligible",
        reason="eligible",
    )
    enabler = SimpleNamespace(
        inspect_auction=lambda auction: inspection,
        resolve_source=lambda inspection: source,
        discover_tokens=lambda **kwargs: TokenDiscovery(tokens_by_address={eligible_probe.token_address: {"manual"}}, notes=[]),
        probe_tokens=lambda **kwargs: [eligible_probe],
        build_execution_plan=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("governance mismatch")),
    )

    monkeypatch.setattr("tidal.api.services.action_prepare.build_sync_web3", lambda settings: object())
    monkeypatch.setattr("tidal.api.services.action_prepare.AuctionTokenEnabler", lambda w3, settings: enabler)

    status, warnings, data = await prepare_enable_tokens_action(
        settings=SimpleNamespace(
            chain_id=1,
            txn_max_gas_limit=500000,
        ),
        session=object(),
        operator_id="tester",
        auction_address=inspection.auction_address,
        sender="0x6666666666666666666666666666666666666666",
        extra_tokens=[],
    )

    assert status == "error"
    assert warnings == ["governance mismatch"]
    assert data["transactions"] == []


@pytest.mark.asyncio
async def test_prepare_kick_action_falls_back_to_extended_kick_for_active_auction(monkeypatch) -> None:
    candidate = KickCandidate(
        source_type="fee_burner",
        source_address="0x1111111111111111111111111111111111111111",
        token_address="0x2222222222222222222222222222222222222222",
        auction_address="0x3333333333333333333333333333333333333333",
        normalized_balance="1000",
        price_usd="1.0",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=1000.0,
        decimals=18,
        source_name="Fee Burner",
        token_symbol="CRV",
        want_symbol="crvUSD",
    )
    prepared = PreparedKick(
        candidate=candidate,
        sell_amount=10**21,
        starting_price_unscaled=1100,
        minimum_price_scaled_1e18=950_000_000_000_000_000,
        minimum_quote_unscaled=950,
        sell_amount_str="1000",
        starting_price_unscaled_str="1100",
        minimum_price_scaled_1e18_str="950000000000000000",
        minimum_quote_unscaled_str="950",
        usd_value_str="1000",
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="1000",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
        step_decay_rate_bps=25,
        pricing_profile_name="volatile",
        settle_token=None,
    )
    recovered = replace(
        prepared,
        recovery_plan=SimpleNamespace(
            is_empty=False,
            settle_after_start=("0x5555555555555555555555555555555555555555",),
            settle_after_min=(),
            settle_after_decay=(),
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
        candidate=candidate,
        prepared=prepared,
        recovered=recovered,
        single_data="0xdeadbeef",
        batch_data="0xdeadbeef",
        extended_data="0xfeedface",
    )

    monkeypatch.setattr(
        "tidal.api.services.action_prepare.build_txn_service",
        lambda settings, session, **kwargs: SimpleNamespace(
            planner=_build_kick_planner(
                shortlist=shortlist,
                deps=deps,
                estimate_transaction_fn=estimate_mock,
            )
        ),
    )
    estimate_mock = AsyncMock(
        side_effect=[
            (None, None, "Gas estimate failed: call to 0x3333…3333 failed: active auction"),
            (210000, 252000, None),
        ]
    )
    monkeypatch.setattr("tidal.api.services.action_prepare._estimate_transaction", estimate_mock)
    monkeypatch.setattr("tidal.api.services.action_prepare.create_prepared_action", lambda *args, **kwargs: "action-1")

    status, warnings, data = await prepare_kick_action(
        session=object(),
        settings=_kick_settings(),
        operator_id="tester",
        source_type="fee_burner",
        source_address=candidate.source_address,
        auction_address=candidate.auction_address,
        token_address=candidate.token_address,
        limit=1,
        sender="0x6666666666666666666666666666666666666666",
        require_curve_quote=None,
    )

    assert status == "ok"
    assert warnings == []
    assert data["transactions"][0]["data"] == "0xfeedface"
    assert data["preview"]["preparedOperations"][0]["recoveryPlan"] == {
        "settleAfterStart": ["0x5555555555555555555555555555555555555555"],
        "settleAfterMin": [],
        "settleAfterDecay": [],
    }


@pytest.mark.asyncio
async def test_load_strategy_deploy_defaults_includes_receiver_address(monkeypatch) -> None:
    strategy_address = "0x1111111111111111111111111111111111111111"
    want_address = "0x2222222222222222222222222222222222222222"
    factory_address = "0x3333333333333333333333333333333333333333"
    governance_address = "0x4444444444444444444444444444444444444444"
    predicted_address = "0x5555555555555555555555555555555555555555"

    class _FakeMappings:
        def all(self):  # noqa: ANN201
            return [
                {
                    "strategy_address": strategy_address,
                    "strategy_name": "Test Strategy",
                    "auction_address": None,
                    "want_address": want_address,
                    "want_symbol": "crvUSD",
                    "active": True,
                    "token_address": "0x6666666666666666666666666666666666666666",
                    "raw_balance": "1000000000000000000000",
                    "normalized_balance": "1000",
                    "token_symbol": "CRV",
                    "token_decimals": 18,
                    "token_price_usd": "1.0",
                }
            ]

    class _FakeExecuteResult:
        def mappings(self):  # noqa: ANN201
            return _FakeMappings()

    class _FakeSession:
        def execute(self, stmt, params):  # noqa: ANN001, ANN201
            del stmt
            assert params["strategy_address"] == strategy_address
            return _FakeExecuteResult()

    class _FakeQuote:
        amount_out_raw = 500_000_000
        token_out_decimals = 6
        request_url = "https://prices.example.com/v1/quote"
        provider_statuses = {"curve": "ok"}

        def curve_quote_available(self) -> bool:
            return True

    class _FakeQuoteProvider:
        def __init__(self, **kwargs):  # noqa: ANN003
            del kwargs

        async def quote(self, **kwargs):  # noqa: ANN003, ANN201
            assert kwargs["token_in"] == "0x6666666666666666666666666666666666666666"
            assert kwargs["token_out"] == want_address
            return _FakeQuote()

        async def close(self) -> None:
            return None

    monkeypatch.setattr("tidal.api.services.action_prepare.TokenPriceAggProvider", _FakeQuoteProvider)
    monkeypatch.setattr("tidal.api.services.action_prepare.build_sync_web3", lambda settings: object())
    monkeypatch.setattr("tidal.api.services.action_prepare.default_factory_address", lambda settings: factory_address)
    monkeypatch.setattr("tidal.api.services.action_prepare.default_governance_address", lambda: governance_address)
    monkeypatch.setattr("tidal.api.services.action_prepare.read_factory_auction_addresses", lambda w3, factory: [])
    monkeypatch.setattr("tidal.api.services.action_prepare.read_existing_matches", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "tidal.api.services.action_prepare.preview_deployment",
        lambda *args, **kwargs: SimpleNamespace(
            predicted_address=predicted_address,
            predicted_address_exists=False,
        ),
    )

    data = await load_strategy_deploy_defaults(
        _FakeSession(),
        SimpleNamespace(
            chain_id=1,
            token_price_agg_base_url="https://prices.example.com",
            token_price_agg_key=None,
            price_timeout_seconds=10,
            price_retry_attempts=1,
            txn_start_price_buffer_bps=1000,
        ),
        strategy_address=strategy_address,
    )

    assert data["strategyAddress"] == strategy_address
    assert data["receiverAddress"] == strategy_address
    assert data["factoryAddress"] == factory_address
    assert data["predictedAuctionAddress"] == predicted_address


@pytest.mark.asyncio
async def test_prepare_deploy_browser_action_is_stateless(monkeypatch) -> None:
    want_address = "0x1111111111111111111111111111111111111111"
    receiver_address = "0x2222222222222222222222222222222222222222"
    sender_address = "0x3333333333333333333333333333333333333333"
    factory_address = "0x4444444444444444444444444444444444444444"
    governance_address = "0x5555555555555555555555555555555555555555"
    predicted_address = "0x6666666666666666666666666666666666666666"

    class _FakeCreateNewAuctionFn:
        def _encode_transaction_data(self) -> str:
            return "0xdeadbeef"

    class _FakeFunctions:
        def createNewAuction(self, *args):  # noqa: ANN002, ANN003
            return _FakeCreateNewAuctionFn()

    class _FakeEth:
        def contract(self, address, abi):  # noqa: ANN001, ANN201
            del address, abi
            return SimpleNamespace(functions=_FakeFunctions())

    monkeypatch.setattr(
        "tidal.api.services.action_prepare.build_sync_web3",
        lambda settings: SimpleNamespace(eth=_FakeEth()),
    )
    monkeypatch.setattr(
        "tidal.api.services.action_prepare.preview_deployment",
        lambda *args, **kwargs: SimpleNamespace(
            preview_error=None,
            gas_error=None,
            gas_estimate=210000,
            predicted_address=predicted_address,
            predicted_address_exists=False,
            existing_matches=[],
        ),
    )
    monkeypatch.setattr(
        "tidal.api.services.action_prepare.create_prepared_action",
        lambda *args, **kwargs: pytest.fail("browser deploy prepare should not create action rows"),
    )

    status, warnings, data = await prepare_deploy_browser_action(
        SimpleNamespace(
            chain_id=1,
            txn_max_gas_limit=500000,
        ),
        want=want_address,
        receiver=receiver_address,
        sender=sender_address,
        factory=factory_address,
        governance=governance_address,
        starting_price=610,
        salt="0x" + "11" * 32,
    )

    assert status == "ok"
    assert warnings == []
    assert "actionId" not in data
    assert data["actionType"] == "deploy"
    assert data["preview"]["predictedAuctionAddress"] == predicted_address
    assert data["transactions"][0]["to"] == factory_address
