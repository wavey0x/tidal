from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.api.services.action_prepare import _estimate_transaction, prepare_kick_action
from tidal.transaction_service.types import KickAction, KickCandidate, PreparedKick


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
        starting_price_raw=2750,
        minimum_price_raw=2375,
        sell_amount_str="1000",
        starting_price_str="2750",
        minimum_price_str="2375",
        usd_value_str="2500",
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="2500",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
        step_decay_rate_bps=50,
        pricing_profile_name="stable",
        settle_token=None,
    )

    shortlist = SimpleNamespace(
        selected_candidates=[candidate],
        eligible_candidates=[candidate],
        deferred_same_auction_count=0,
        limited_candidates=[],
    )
    monkeypatch.setattr("tidal.api.services.action_prepare.build_shortlist", lambda *args, **kwargs: shortlist)
    monkeypatch.setattr(
        "tidal.api.services.action_prepare.check_pre_send",
        lambda candidates, **kwargs: [SimpleNamespace(action=KickAction.KICK, candidate=item) for item in candidates],
    )
    monkeypatch.setattr("tidal.api.services.action_prepare.sort_candidates", lambda candidates: candidates)

    class _FakeBatchKickFn:
        def _encode_transaction_data(self) -> str:
            return "0xdeadbeef"

    class _FakeFunctions:
        def batchKick(self, kick_tuples):  # noqa: ANN001
            assert kick_tuples == [()]
            return _FakeBatchKickFn()

    fake_web3 = SimpleNamespace(contract=lambda address, abi: SimpleNamespace(functions=_FakeFunctions()))
    fake_kicker = SimpleNamespace(
        inspect_candidates=AsyncMock(return_value={(candidate.auction_address, candidate.token_address): None}),
        prepare_kick=AsyncMock(return_value=prepared),
        web3_client=fake_web3,
        _kick_args=lambda prepared_kick: (),
    )

    captured: dict[str, object] = {}

    def fake_build_txn_service(settings, session, **kwargs):  # noqa: ANN001, ANN003
        del settings, session
        captured["require_curve_quote"] = kwargs.get("require_curve_quote")
        return SimpleNamespace(kicker=fake_kicker)

    monkeypatch.setattr("tidal.api.services.action_prepare.build_txn_service", fake_build_txn_service)
    monkeypatch.setattr(
        "tidal.api.services.action_prepare._estimate_transaction",
        AsyncMock(return_value=(210000, 252000, None)),
    )
    monkeypatch.setattr("tidal.api.services.action_prepare.create_prepared_action", lambda *args, **kwargs: "action-1")

    status, warnings, data = await prepare_kick_action(
        session=object(),
        settings=SimpleNamespace(
            txn_usd_threshold=100.0,
            txn_max_data_age_seconds=600,
            txn_cooldown_seconds=3600,
            txn_max_gas_limit=500000,
            auction_kicker_address="0x5555555555555555555555555555555555555555",
            chain_id=1,
        ),
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
