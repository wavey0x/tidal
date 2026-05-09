from tidal.transaction_service.types import (
    KickCandidate,
    KickPlan,
    PreparedKick,
    PreparedResolveAuction,
    SkippedPreparedCandidate,
    TxIntent,
)


def _candidate(*, token_address: str, token_symbol: str = "CRV") -> KickCandidate:
    return KickCandidate(
        source_type="strategy",
        source_address="0x1111111111111111111111111111111111111111",
        token_address=token_address,
        auction_address="0x3333333333333333333333333333333333333333",
        normalized_balance="1000",
        price_usd="2.5",
        want_address="0x4444444444444444444444444444444444444444",
        usd_value=2500.0,
        decimals=18,
        source_name="Test Strategy",
        token_symbol=token_symbol,
        want_symbol="USDC",
    )


def _prepared_kick(candidate: KickCandidate) -> PreparedKick:
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
        usd_value_str="2500",
        live_balance_raw=10**21,
        normalized_balance="1000",
        quote_amount_str="2500",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=50,
        step_decay_rate_bps=50,
        pricing_profile_name="stable",
    )


def test_tx_intent_round_trips_payload() -> None:
    intent = TxIntent(
        operation="kick",
        to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        data="0xdeadbeef",
        value="0x0",
        chain_id=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        gas_estimate=210000,
        gas_limit=252000,
    )

    payload = intent.to_payload()

    assert payload == {
        "operation": "kick",
        "to": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "data": "0xdeadbeef",
        "value": "0x0",
        "chainId": 1,
        "sender": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "gasEstimate": 210000,
        "gasLimit": 252000,
    }
    assert TxIntent.from_payload(payload) == intent


def test_kick_plan_serializes_resolve_and_kick_operations() -> None:
    kick_candidate = _candidate(token_address="0x2222222222222222222222222222222222222222")
    stale_candidate = _candidate(
        token_address="0x6666666666666666666666666666666666666666",
        token_symbol="YFI",
    )
    prepared_kick = _prepared_kick(kick_candidate)
    prepared_resolve = PreparedResolveAuction(
        candidate=kick_candidate,
        sell_token="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        path=5,
        reason="inactive kicked lot with stranded inventory",
        balance_raw=10**21,
        requires_force=False,
        receiver="0x5555555555555555555555555555555555555555",
        token_symbol="CRV",
        normalized_balance="1000",
    )
    resolve_intent = TxIntent(
        operation="resolve-auction",
        to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        data="0xaaaa",
        value="0x0",
        chain_id=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        gas_estimate=180000,
        gas_limit=216000,
    )
    kick_intent = TxIntent(
        operation="kick",
        to="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        data="0xbbbb",
        value="0x0",
        chain_id=1,
        sender="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        gas_estimate=210000,
        gas_limit=252000,
    )
    plan = KickPlan(
        source_type="strategy",
        source_address=kick_candidate.source_address,
        auction_address=kick_candidate.auction_address,
        token_address=None,
        limit=2,
        eligible_count=4,
        selected_count=3,
        ready_count=2,
        kick_operations=[prepared_kick],
        resolve_operations=[prepared_resolve],
        tx_intents=[resolve_intent, kick_intent],
        skipped_during_prepare=[
            SkippedPreparedCandidate(
                candidate=stale_candidate,
                reason="auction still active with live sell balance",
            )
        ],
        warnings=["Curve quote unavailable"],
    )

    preview = plan.to_preview_payload()

    assert plan.status() == "ok"
    assert plan.to_transaction_payloads() == [resolve_intent.to_payload(), kick_intent.to_payload()]
    assert preview["preparedOperations"][0] == {
        "operation": "resolve-auction",
        "txIndex": 0,
        "auctionAddress": "0x3333333333333333333333333333333333333333",
        "sourceAddress": "0x1111111111111111111111111111111111111111",
        "sourceName": "Test Strategy",
        "sourceType": "strategy",
        "tokenAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "tokenSymbol": "CRV",
        "wantAddress": "0x4444444444444444444444444444444444444444",
        "wantSymbol": "USDC",
        "reason": "inactive kicked lot with stranded inventory",
        "path": 5,
        "requiresForce": False,
        "balanceRaw": str(10**21),
        "normalizedBalance": "1000",
        "receiver": "0x5555555555555555555555555555555555555555",
    }
    assert preview["preparedOperations"][1]["operation"] == "kick"
    assert preview["preparedOperations"][1]["txIndex"] == 1
    assert preview["skippedDuringPrepare"][0]["reason"] == "auction still active with live sell balance"


def test_kick_plan_status_is_noop_without_transactions() -> None:
    plan = KickPlan(
        source_type=None,
        source_address=None,
        auction_address=None,
        token_address=None,
        limit=None,
        eligible_count=0,
        selected_count=0,
        ready_count=0,
    )

    assert plan.status() == "noop"
    assert plan.to_transaction_payloads() == []
