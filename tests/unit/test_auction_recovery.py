from tidal.transaction_service.auction_recovery import (
    AuctionGlobals,
    AuctionTokenState,
    build_recovery_plan,
)


def test_build_recovery_plan_detects_revival_after_starting_price() -> None:
    token = AuctionTokenState(
        token_address="0x1111111111111111111111111111111111111111",
        kicked_at=1,
        scaler=1,
        initial_available=10**21,
        auction_balance=0,
    )
    current = AuctionGlobals(
        starting_price=100,
        minimum_price=200_000_000_000_000_000,
        step_decay_rate=1,
        step_duration=60,
        auction_length=86_400,
        want_scaler=1,
    )

    plan = build_recovery_plan(
        [token],
        current,
        timestamp=1,
        proposed_starting_price=250,
        proposed_minimum_price=current.minimum_price,
        proposed_step_decay_rate=current.step_decay_rate,
    )

    assert plan.settle_after_start == (token.token_address,)
    assert plan.settle_after_min == ()
    assert plan.settle_after_decay == ()


def test_build_recovery_plan_detects_revival_after_minimum_price() -> None:
    token = AuctionTokenState(
        token_address="0x2222222222222222222222222222222222222222",
        kicked_at=1,
        scaler=1,
        initial_available=10**21,
        auction_balance=0,
    )
    current = AuctionGlobals(
        starting_price=100,
        minimum_price=200_000_000_000_000_000,
        step_decay_rate=1,
        step_duration=60,
        auction_length=86_400,
        want_scaler=1,
    )

    plan = build_recovery_plan(
        [token],
        current,
        timestamp=1,
        proposed_starting_price=current.starting_price,
        proposed_minimum_price=50_000_000_000_000_000,
        proposed_step_decay_rate=current.step_decay_rate,
    )

    assert plan.settle_after_start == ()
    assert plan.settle_after_min == (token.token_address,)
    assert plan.settle_after_decay == ()
