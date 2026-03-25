"""Unit tests for transaction service evaluator."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import KickTxRepository
from factory_dashboard.transaction_service.evaluator import check_pre_send, shortlist_candidates
from factory_dashboard.transaction_service.types import KickCandidate


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


def test_check_pre_send_allows_kick(session):
    repo = KickTxRepository(session)
    candidates = [_make_candidate()]
    decisions = check_pre_send(
        candidates, kick_tx_repository=repo, cooldown_seconds=3600
    )
    assert len(decisions) == 1
    assert decisions[0].action == "KICK"


def test_check_pre_send_cooldown_blocks(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    # Insert a recent CONFIRMED kick.
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "CONFIRMED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    candidates = [_make_candidate()]
    decisions = check_pre_send(
        candidates, kick_tx_repository=repo, cooldown_seconds=3600
    )
    assert len(decisions) == 1
    assert decisions[0].action == "SKIP"
    assert decisions[0].skip_reason == "COOLDOWN"


def test_check_pre_send_submitted_blocks(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "SUBMITTED",
        "tx_hash": "0xabc",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    candidates = [_make_candidate()]
    decisions = check_pre_send(
        candidates, kick_tx_repository=repo, cooldown_seconds=3600
    )
    assert decisions[0].action == "SKIP"
    assert decisions[0].skip_reason == "COOLDOWN"


def test_check_pre_send_reverted_does_not_block(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "REVERTED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    candidates = [_make_candidate()]
    decisions = check_pre_send(
        candidates, kick_tx_repository=repo, cooldown_seconds=3600
    )
    assert decisions[0].action == "KICK"


def test_check_pre_send_expired_cooldown_allows(session):
    models.metadata.create_all(session.get_bind())
    repo = KickTxRepository(session)
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    repo.insert({
        "run_id": "old-run",
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "CONFIRMED",
        "created_at": old_time,
    })

    candidates = [_make_candidate()]
    decisions = check_pre_send(
        candidates, kick_tx_repository=repo, cooldown_seconds=3600
    )
    assert decisions[0].action == "KICK"
