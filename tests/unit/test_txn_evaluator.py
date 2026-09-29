"""Unit tests for transaction service evaluator."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository
from tidal.transaction_service.evaluator import build_shortlist, shortlist_candidates
from tidal.transaction_service.kick_policy import CooldownPolicy, IgnorePolicy
from tidal.transaction_service.types import KickCandidate


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_data(session, *, auction_address="0xauction", want_address="0xwant1", price_status="SUCCESS", price_usd="2.5", scanned_at=None, price_fetched_at=None, strategy_name="Test Strategy", token_symbol="TKN", want_symbol="USDC"):
    now = datetime.now(timezone.utc)
    scanned_at = scanned_at or now.isoformat()
    price_fetched_at = price_fetched_at or now.isoformat()

    session.execute(insert(models.strategies).values(
        address="0xstrategy1",
        chain_id=1,
        vault_address="0xvault1",
        name=strategy_name,
        adapter="yearn_curve_strategy",
        active=1,
        auction_address=auction_address,
        want_address=want_address,
        first_seen_at=now.isoformat(),
        last_seen_at=now.isoformat(),
    ))
    session.execute(insert(models.tokens).values(
        address="0xtoken1",
        chain_id=1,
        symbol=token_symbol,
        decimals=18,
        is_core_reward=1,
        price_usd=price_usd,
        price_status=price_status,
        price_fetched_at=price_fetched_at,
        first_seen_at=now.isoformat(),
        last_seen_at=now.isoformat(),
    ))
    # Seed want token row so LEFT JOIN picks up want_symbol.
    if want_address is not None and want_symbol is not None:
        session.execute(insert(models.tokens).values(
            address=want_address,
            chain_id=1,
            symbol=want_symbol,
            decimals=6,
            is_core_reward=0,
            first_seen_at=now.isoformat(),
            last_seen_at=now.isoformat(),
        ))
    session.execute(insert(models.strategy_token_balances_latest).values(
        strategy_address="0xstrategy1",
        token_address="0xtoken1",
        raw_balance="1000000000000000000000",
        normalized_balance="1000",
        block_number=100,
        scanned_at=scanned_at,
    ))
    session.commit()


def test_shortlist_returns_candidates_above_threshold(session):
    _seed_data(session)
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 1
    assert candidates[0].strategy_address == "0xstrategy1"
    assert candidates[0].usd_value == pytest.approx(2500.0)
    assert candidates[0].want_address == "0xwant1"
    assert candidates[0].strategy_name == "Test Strategy"
    assert candidates[0].token_symbol == "TKN"
    assert candidates[0].want_symbol == "USDC"


def test_shortlist_filters_below_threshold(session):
    _seed_data(session)
    candidates = shortlist_candidates(session, usd_threshold=5000, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_by_token_address_case_insensitively(session):
    _seed_data(session)
    candidates = shortlist_candidates(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        token_address="0XTOKEN1",
    )
    assert len(candidates) == 1
    assert candidates[0].token_address == "0xtoken1"


def test_shortlist_filters_no_auction(session):
    _seed_data(session, auction_address=None)
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_failed_price_status(session):
    _seed_data(session, price_status="FAILED")
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_null_price(session):
    _seed_data(session, price_usd=None, price_status="SUCCESS")
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_stale_scan(session):
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    _seed_data(session, scanned_at=old_time)
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_stale_price(session):
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    _seed_data(session, price_fetched_at=old_time)
    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    assert len(candidates) == 0


def test_shortlist_filters_cached_disabled_auction_tokens(session):
    now = datetime.now(timezone.utc).isoformat()
    _seed_data(session, auction_address="0xauction_enabled")
    session.execute(insert(models.auction_enabled_token_scans).values(
        auction_address="0xauction_enabled",
        scanned_at=now,
        block_number=123,
        status="SUCCESS",
        error_message=None,
    ))
    session.commit()

    candidates = shortlist_candidates(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
    )

    assert len(candidates) == 0


def test_shortlist_includes_fee_burner_candidates(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)
    fee_burner_candidate = next(candidate for candidate in candidates if candidate.source_type == "fee_burner")

    assert fee_burner_candidate.source_address == "0xburner1"
    assert fee_burner_candidate.source_name == "Yearn Fee Burner"
    assert fee_burner_candidate.want_address == "0xwantfb"
    assert fee_burner_candidate.want_symbol == "crvUSD"


def test_shortlist_filters_to_strategy_type(session):
    _seed_data(session)

    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        source_type="strategy",
    )

    assert len(candidates) == 1
    assert candidates[0].source_type == "strategy"


def test_shortlist_filters_to_fee_burner_type(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        source_type="fee_burner",
    )

    assert len(candidates) == 1
    assert candidates[0].source_type == "fee_burner"


def test_shortlist_excludes_token_matching_want(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        price_usd="1.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xwantfb",
        raw_balance="100000000000000000000",
        normalized_balance="100",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(session, usd_threshold=10, max_data_age_seconds=600)
    assert candidates == []


def test_shortlist_keeps_highest_usd_candidate_per_auction(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb1",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb2",
        chain_id=1,
        symbol="CRV",
        decimals=18,
        is_core_reward=0,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb1",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb2",
        raw_balance="200000000000000000000",
        normalized_balance="200",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)

    assert len(candidates) == 1
    assert candidates[0].auction_address == "0xauctionfb"
    assert candidates[0].token_address == "0xtokenfb1"
    assert candidates[0].usd_value == pytest.approx(500.0)


def test_build_shortlist_reports_same_auction_deferrals(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb1",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb2",
        chain_id=1,
        symbol="CRV",
        decimals=18,
        is_core_reward=0,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb1",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb2",
        raw_balance="200000000000000000000",
        normalized_balance="200",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    shortlist = build_shortlist(session, usd_threshold=100, max_data_age_seconds=600)

    assert len(shortlist.eligible_candidates) == 2
    assert len(shortlist.selected_candidates) == 1
    assert shortlist.deferred_same_auction_count == 1
    assert shortlist.selected_candidates[0].token_address == "0xtokenfb1"


def test_shortlist_orders_candidates_by_descending_usd_value(session):
    now = datetime.now(timezone.utc).isoformat()

    session.execute(insert(models.vaults).values(
        address="0xvault1",
        chain_id=1,
        symbol="v1",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.vaults).values(
        address="0xvault2",
        chain_id=1,
        symbol="v2",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.strategies).values(
        address="0xstrategy1",
        chain_id=1,
        vault_address="0xvault1",
        name="Strategy One",
        adapter="yearn_curve_strategy",
        active=1,
        auction_address="0xauction1",
        want_address="0xwant1",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.strategies).values(
        address="0xstrategy2",
        chain_id=1,
        vault_address="0xvault2",
        name="Strategy Two",
        adapter="yearn_curve_strategy",
        active=1,
        auction_address="0xauction2",
        want_address="0xwant2",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtoken1",
        chain_id=1,
        symbol="AAA",
        decimals=18,
        is_core_reward=1,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtoken2",
        chain_id=1,
        symbol="BBB",
        decimals=18,
        is_core_reward=1,
        price_usd="1.5",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwant1",
        chain_id=1,
        symbol="USDC",
        decimals=6,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwant2",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.strategy_token_balances_latest).values(
        strategy_address="0xstrategy1",
        token_address="0xtoken1",
        raw_balance="100000000000000000000",
        normalized_balance="100",
        block_number=100,
        scanned_at=now,
    ))
    session.execute(insert(models.strategy_token_balances_latest).values(
        strategy_address="0xstrategy2",
        token_address="0xtoken2",
        raw_balance="200000000000000000000",
        normalized_balance="200",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    candidates = shortlist_candidates(session, usd_threshold=100, max_data_age_seconds=600)

    assert [candidate.token_address for candidate in candidates] == ["0xtoken2", "0xtoken1"]
    assert [candidate.usd_value for candidate in candidates] == pytest.approx([300.0, 200.0])


def _make_candidate(**overrides):
    defaults = {
        "source_type": "strategy",
        "source_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "normalized_balance": "1000",
        "price_usd": "2.5",
        "want_address": "0xwant1",
        "usd_value": 2500.0,
        "decimals": 18,
    }
    defaults.update(overrides)
    return KickCandidate(**defaults)


def test_build_shortlist_ignore_source_blocks(session):
    _seed_data(session)
    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        ignore_policy=IgnorePolicy(
            ignored_sources=frozenset({"0xstrategy1"}),
            ignored_auctions=frozenset(),
            ignored_auction_tokens=frozenset(),
        ),
    )

    assert shortlist.selected_candidates == []
    assert len(shortlist.ignored_skips) == 1
    assert shortlist.ignored_skips[0].skip_reason == "IGNORED"


def test_build_shortlist_ignore_auction_token_blocks(session):
    _seed_data(session)
    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        ignore_policy=IgnorePolicy(
            ignored_sources=frozenset(),
            ignored_auctions=frozenset(),
            ignored_auction_tokens=frozenset({("0xauction", "0xtoken1")}),
        ),
    )

    assert shortlist.selected_candidates == []
    assert len(shortlist.ignored_skips) == 1
    assert shortlist.ignored_skips[0].detail == "ignored by auction/token rule"


def test_build_shortlist_cooldown_blocks(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    _seed_data(session, auction_address="0xauction1")
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "CONFIRMED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        kick_tx_repository=repo,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
    )

    assert shortlist.selected_candidates == []
    assert len(shortlist.cooldown_skips) == 1
    assert shortlist.cooldown_skips[0].skip_reason == "COOLDOWN"


def test_build_shortlist_submitted_blocks(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    _seed_data(session, auction_address="0xauction1")
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "SUBMITTED",
        "tx_hash": "0xabc",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        kick_tx_repository=repo,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
    )
    assert shortlist.selected_candidates == []
    assert len(shortlist.cooldown_skips) == 1
    assert shortlist.cooldown_skips[0].skip_reason == "COOLDOWN"


def test_build_shortlist_reverted_does_not_block(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    _seed_data(session, auction_address="0xauction1")
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "REVERTED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        kick_tx_repository=repo,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
    )
    assert len(shortlist.selected_candidates) == 1
    assert shortlist.cooldown_skips == []


def test_build_shortlist_expired_cooldown_allows(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    _seed_data(session, auction_address="0xauction1")
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "CONFIRMED",
        "created_at": old_time,
    })

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        kick_tx_repository=repo,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
    )
    assert len(shortlist.selected_candidates) == 1
    assert shortlist.cooldown_skips == []


@pytest.mark.parametrize("next_status,blocked", [
    (None, False), ("SUBMITTED", True), ("CONFIRMED", True),
    ("REVERTED", False), ("DRY_RUN", False), ("SKIP", False), ("ERROR", False),
])
def test_cleared_latest_kick_does_not_resurrect_older_cooldown_and_new_kick_rearms(session, next_status, blocked):
    _seed_data(session, auction_address="0xauction1")
    repo = KickTxRepository(session)
    now = datetime.now(timezone.utc).isoformat()
    row = dict(run_id="old-run", token_address="0xtoken1", auction_address="0xauction1",
               status="CONFIRMED", created_at=now)
    # Equal timestamps must still select the newer ID. The older cooldown
    # remains uncleared and must never reappear behind the cleared latest kick.
    repo.insert(row)
    repo.insert({**row, "cooldown_cleared_at": now})
    if next_status:
        repo.insert({**row, "run_id": "new-run", "status": next_status})
    options = dict(usd_threshold=100, max_data_age_seconds=600, kick_tx_repository=repo,
                   cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}))
    for _ in range(2):
        shortlist = build_shortlist(session, **options)
        assert bool(shortlist.cooldown_skips) is blocked
        assert bool(shortlist.selected_candidates) is not blocked
        session.commit()
    # A clear only changes cooldown eligibility, not the minimum value rule.
    options["usd_threshold"] = 3000
    assert build_shortlist(session, **options).selected_candidates == []


def test_build_shortlist_ignored_candidate_allows_next_same_auction_candidate(session):
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.fee_burners).values(
        address="0xburner1",
        chain_id=1,
        name="Yearn Fee Burner",
        active=1,
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb1",
        chain_id=1,
        symbol="YFI",
        decimals=18,
        is_core_reward=0,
        price_usd="10.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xtokenfb2",
        chain_id=1,
        symbol="CRV",
        decimals=18,
        is_core_reward=0,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address="0xwantfb",
        chain_id=1,
        symbol="crvUSD",
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb1",
        raw_balance="50000000000000000000",
        normalized_balance="50",
        block_number=101,
        scanned_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address="0xburner1",
        token_address="0xtokenfb2",
        raw_balance="200000000000000000000",
        normalized_balance="200",
        block_number=101,
        scanned_at=now,
    ))
    session.commit()

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        ignore_policy=IgnorePolicy(
            ignored_sources=frozenset(),
            ignored_auctions=frozenset(),
            ignored_auction_tokens=frozenset({("0xauctionfb", "0xtokenfb1")}),
        ),
    )

    assert len(shortlist.ignored_skips) == 1
    assert len(shortlist.selected_candidates) == 1
    assert shortlist.selected_candidates[0].token_address == "0xtokenfb2"


def test_build_shortlist_pair_cooldown_override_zero_disables_default(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    _seed_data(session, auction_address="0xauction1")
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "CONFIRMED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    shortlist = build_shortlist(
        session,
        usd_threshold=100,
        max_data_age_seconds=600,
        kick_tx_repository=repo,
        cooldown_policy=CooldownPolicy(
            default_minutes=60,
            auction_token_overrides_minutes={("0xauction1", "0xtoken1"): 0},
        ),
    )

    assert len(shortlist.selected_candidates) == 1
    assert shortlist.cooldown_skips == []
