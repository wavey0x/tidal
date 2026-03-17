"""Integration tests for TxnService."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.persistence.repositories import KickTxRepository, TxnRunRepository
from factory_dashboard.transaction_service.service import TxnService
from factory_dashboard.transaction_service.types import KickCandidate, KickResult, KickStatus, PreparedKick


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    models.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _seed_candidate(session, *, strategy_address="0xstrategy1", token_address="0xtoken1",
                    auction_address="0xauction1", want_address="0xwant1",
                    price_usd="2.5", normalized_balance="1000"):
    now = datetime.now(timezone.utc).isoformat()

    session.execute(insert(models.strategies).values(
        address=strategy_address,
        chain_id=1,
        vault_address="0xvault1",
        adapter="yearn_curve_strategy",
        active=1,
        auction_address=auction_address,
        want_address=want_address,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address=token_address,
        chain_id=1,
        decimals=18,
        is_core_reward=1,
        price_usd=price_usd,
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.strategy_token_balances_latest).values(
        strategy_address=strategy_address,
        token_address=token_address,
        raw_balance="1000000000000000000000",
        normalized_balance=normalized_balance,
        block_number=100,
        scanned_at=now,
    ))
    session.commit()


def _make_prepared_kick(candidate: KickCandidate) -> PreparedKick:
    """Build a PreparedKick from a KickCandidate for test mocks."""
    return PreparedKick(
        candidate=candidate,
        sell_amount=1000000000000000000000,
        starting_price_raw=1000,
        minimum_price_raw=900,
        sell_amount_str="1000000000000000000000",
        starting_price_str="1000",
        minimum_price_str="900",
        usd_value_str=str(candidate.usd_value),
        live_balance_raw=1000000000000000000000,
        normalized_balance=candidate.normalized_balance,
        quote_amount_str="1000",
    )


def _build_txn_service(session, *, kicker=None, lock_path=None):
    txn_run_repo = TxnRunRepository(session)
    kick_tx_repo = KickTxRepository(session)

    if kicker is None:
        kicker = MagicMock()

    if lock_path is None:
        lock_path = Path("/tmp/test_txn_daemon.lock")

    return TxnService(
        session=session,
        kicker=kicker,
        txn_run_repository=txn_run_repo,
        kick_tx_repository=kick_tx_repo,
        usd_threshold=100.0,
        max_data_age_seconds=600,
        cooldown_seconds=3600,
        lock_path=lock_path,
    )


@pytest.mark.asyncio
async def test_dry_run_persists_dry_run_rows(session):
    """Dry-run should write DRY_RUN kick_txs rows and finalize txn_runs."""
    _seed_candidate(session)

    service = _build_txn_service(session)
    result = await service.run_once(live=False)

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 1
    assert result.kicks_attempted == 1

    # Check txn_runs row.
    txn_runs = session.execute(select(models.txn_runs)).mappings().all()
    assert len(txn_runs) == 1
    assert txn_runs[0]["status"] == "DRY_RUN"
    assert txn_runs[0]["candidates_found"] == 1

    # Check kick_txs row.
    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["status"] == "DRY_RUN"
    assert kick_txs[0]["strategy_address"] == "0xstrategy1"
    assert kick_txs[0]["token_address"] == "0xtoken1"


@pytest.mark.asyncio
async def test_dry_run_no_candidates(session):
    """When no candidates exist, dry-run should still finalize cleanly."""
    service = _build_txn_service(session)
    result = await service.run_once(live=False)

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 0
    assert result.kicks_attempted == 0


@pytest.mark.asyncio
async def test_dry_run_filters_below_threshold(session):
    """Candidates below threshold should not produce kick_txs rows."""
    _seed_candidate(session, price_usd="0.001")  # 0.001 * 1000 = $1

    service = _build_txn_service(session)
    result = await service.run_once(live=False)

    assert result.candidates_found == 0
    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 0


@pytest.mark.asyncio
async def test_live_kick_confirmed(session):
    """Live run with confirmed kick should finalize as SUCCESS."""
    _seed_candidate(session)

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id: _make_prepared_kick(c))
    kicker.execute_batch = AsyncMock(return_value=[KickResult(
        kick_tx_id=1,
        status=KickStatus.CONFIRMED,
        tx_hash="0xabc",
        gas_used=180000,
        block_number=12345,
    )])

    service = _build_txn_service(session, kicker=kicker)
    result = await service.run_once(live=True)

    assert result.status == "SUCCESS"
    assert result.kicks_attempted == 1
    assert result.kicks_succeeded == 1
    assert result.kicks_failed == 0

    txn_runs = session.execute(select(models.txn_runs)).mappings().all()
    assert txn_runs[0]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_live_kick_reverted(session):
    """Live run with reverted kick should finalize as FAILED."""
    _seed_candidate(session)

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id: _make_prepared_kick(c))
    kicker.execute_batch = AsyncMock(return_value=[KickResult(
        kick_tx_id=1,
        status=KickStatus.REVERTED,
        tx_hash="0xdef",
    )])

    service = _build_txn_service(session, kicker=kicker)
    result = await service.run_once(live=True)

    assert result.status == "FAILED"
    assert result.kicks_failed == 1


@pytest.mark.asyncio
async def test_live_skip_below_threshold_not_counted(session):
    """Live kicker returning SKIP from prepare should decrement kicks_attempted."""
    _seed_candidate(session)

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(return_value=KickResult(
        kick_tx_id=0,
        status=KickStatus.SKIP,
        error_message="below threshold on live balance",
    ))

    service = _build_txn_service(session, kicker=kicker)
    result = await service.run_once(live=True)

    assert result.kicks_attempted == 0
    assert result.kicks_succeeded == 0


@pytest.mark.asyncio
async def test_submitted_blocks_resend(session):
    """A SUBMITTED kick_txs row should block re-sending for same pair."""
    _seed_candidate(session)

    # First run: kick returns SUBMITTED (receipt timeout).
    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id: _make_prepared_kick(c))
    kicker.execute_batch = AsyncMock(return_value=[KickResult(
        kick_tx_id=1,
        status=KickStatus.SUBMITTED,
        tx_hash="0xpending",
    )])

    service = _build_txn_service(session, kicker=kicker)
    result1 = await service.run_once(live=True)
    assert result1.kicks_attempted == 1

    # Manually insert SUBMITTED row (since kicker is mocked, the row wasn't actually written
    # by the service — the kicker would have done it in real life).
    kick_tx_repo = KickTxRepository(session)
    kick_tx_repo.insert({
        "run_id": result1.run_id,
        "strategy_address": "0xstrategy1",
        "token_address": "0xtoken1",
        "auction_address": "0xauction1",
        "status": "SUBMITTED",
        "tx_hash": "0xpending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # Second run: same pair should be skipped due to SUBMITTED cooldown.
    service2 = _build_txn_service(session, kicker=kicker)
    result2 = await service2.run_once(live=False)

    # Candidate found but skipped via cooldown.
    kick_txs = session.execute(
        select(models.kick_txs).where(models.kick_txs.c.run_id == result2.run_id)
    ).mappings().all()
    assert len(kick_txs) == 0  # No DRY_RUN row because it was skipped


@pytest.mark.asyncio
async def test_multiple_candidates_dry_run(session):
    """Multiple candidates should each produce a DRY_RUN row."""
    now = datetime.now(timezone.utc).isoformat()

    # Seed two strategies with different tokens.
    for i in range(1, 3):
        session.execute(insert(models.strategies).values(
            address=f"0xstrategy{i}",
            chain_id=1,
            vault_address="0xvault1",
            adapter="yearn_curve_strategy",
            active=1,
            auction_address=f"0xauction{i}",
            want_address="0xwant1",
            first_seen_at=now,
            last_seen_at=now,
        ))
        session.execute(insert(models.tokens).values(
            address=f"0xtoken{i}",
            chain_id=1,
            decimals=18,
            is_core_reward=1,
            price_usd="5.0",
            price_status="SUCCESS",
            price_fetched_at=now,
            first_seen_at=now,
            last_seen_at=now,
        ))
        session.execute(insert(models.strategy_token_balances_latest).values(
            strategy_address=f"0xstrategy{i}",
            token_address=f"0xtoken{i}",
            raw_balance="1000000000000000000000",
            normalized_balance="1000",
            block_number=100,
            scanned_at=now,
        ))
    session.commit()

    service = _build_txn_service(session)
    result = await service.run_once(live=False)

    assert result.candidates_found == 2
    assert result.kicks_attempted == 2

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 2
    assert all(row["status"] == "DRY_RUN" for row in kick_txs)
