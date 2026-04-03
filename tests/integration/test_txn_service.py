"""Integration tests for TxnService."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from tidal.persistence import models
from tidal.persistence.repositories import KickTxRepository, TxnRunRepository
from tidal.transaction_service.kick_policy import CooldownPolicy, IgnorePolicy
from tidal.transaction_service.service import TxnService
from tidal.transaction_service.types import (
    KickCandidate,
    KickPlan,
    KickResult,
    KickStatus,
    PreparedKick,
    SkippedPreparedCandidate,
    TransactionExecutionReport,
)


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


def _seed_fee_burner_candidate(
    session,
    *,
    burner_address="0xburner1",
    token_address="0xtokenfb",
    auction_address="0xauctionfb",
    want_address="0xwantfb",
    price_usd="10.0",
    normalized_balance="50",
):
    now = datetime.now(timezone.utc).isoformat()

    session.execute(insert(models.fee_burners).values(
        address=burner_address,
        chain_id=1,
        name="Yearn Fee Burner",
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
        is_core_reward=0,
        price_usd=price_usd,
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.tokens).values(
        address=want_address,
        chain_id=1,
        decimals=18,
        is_core_reward=0,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address=burner_address,
        token_address=token_address,
        raw_balance="50000000000000000000",
        normalized_balance=normalized_balance,
        block_number=101,
        scanned_at=now,
    ))
    session.commit()


def _make_prepared_kick(candidate: KickCandidate) -> PreparedKick:
    """Build a PreparedKick from a KickCandidate for test mocks."""
    return PreparedKick(
        candidate=candidate,
        sell_amount=1000000000000000000000,
        starting_price_unscaled=1000,
        minimum_price_scaled_1e18=900000000000000000,
        minimum_quote_unscaled=900,
        sell_amount_str="1000000000000000000000",
        starting_price_unscaled_str="1000",
        minimum_price_scaled_1e18_str="900000000000000000",
        minimum_quote_unscaled_str="900",
        usd_value_str=str(candidate.usd_value),
        live_balance_raw=1000000000000000000000,
        normalized_balance=candidate.normalized_balance,
        quote_amount_str="1000",
        start_price_buffer_bps=1000,
        min_price_buffer_bps=500,
        step_decay_rate_bps=50,
        pricing_profile_name="volatile",
        settle_token=None,
    )


def _evm_address(char: str) -> str:
    return f"0x{char * 40}"


def _build_txn_service(session, *, kicker=None, lock_path=None):
    txn_run_repo = TxnRunRepository(session)
    kick_tx_repo = KickTxRepository(session)

    if kicker is None:
        kicker = MagicMock()
    if not isinstance(getattr(kicker, "inspect_candidates", None), AsyncMock):
        kicker.inspect_candidates = AsyncMock(return_value={})
    if not isinstance(getattr(kicker, "execute_sweep_and_settle", None), AsyncMock):
        kicker.execute_sweep_and_settle = AsyncMock()

    if lock_path is None:
        lock_path = Path("/tmp/test_txn_daemon.lock")

    return TxnService(
        session=session,
        kicker=kicker,
        txn_run_repository=txn_run_repo,
        kick_tx_repository=kick_tx_repo,
        usd_threshold=100.0,
        max_data_age_seconds=600,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
        ignore_policy=IgnorePolicy(
            ignored_sources=frozenset(),
            ignored_auctions=frozenset(),
            ignored_auction_tokens=frozenset(),
        ),
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
    assert kick_txs[0]["source_type"] == "strategy"
    assert kick_txs[0]["source_address"] == "0xstrategy1"
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
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id, inspection=None: _make_prepared_kick(c))
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
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id, inspection=None: _make_prepared_kick(c))
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
async def test_live_non_batch_emits_execution_report_before_next_candidate(session):
    _seed_candidate(session)

    captured_reports: list[TransactionExecutionReport] = []

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id, inspection=None: _make_prepared_kick(c))
    kicker.execute_single = AsyncMock(return_value=KickResult(
        kick_tx_id=1,
        status=KickStatus.CONFIRMED,
        tx_hash="0xabc",
        gas_used=180000,
        block_number=12345,
        execution_report=TransactionExecutionReport(
            operation="kick",
            sender="0x1111111111111111111111111111111111111111",
            tx_hash="0xabc",
            broadcast_at="2026-03-29T19:00:00+00:00",
            chain_id=1,
            gas_estimate=200000,
            receipt_status="CONFIRMED",
            block_number=12345,
            gas_used=180000,
        ),
    ))

    service = _build_txn_service(session, kicker=kicker)
    service.execution_report_fn = captured_reports.append
    result = await service.run_once(live=True, batch=False)

    assert result.status == "SUCCESS"
    assert len(captured_reports) == 1
    assert captured_reports[0].tx_hash == "0xabc"
    assert captured_reports[0].receipt_status == "CONFIRMED"


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
async def test_live_planner_prepare_error_counts_as_failure_and_persists(session):
    _seed_candidate(session)
    txn_run_repo = TxnRunRepository(session)
    kick_tx_repo = KickTxRepository(session)
    candidate = KickCandidate(
        source_type="strategy",
        source_address="0xstrategy1",
        token_address="0xtoken1",
        auction_address="0xauction1",
        normalized_balance="1000",
        price_usd="2.5",
        want_address="0xwant1",
        usd_value=2500.0,
        decimals=18,
    )
    planner = AsyncMock()
    planner.plan = AsyncMock(
        return_value=KickPlan(
            source_type=None,
            source_address=None,
            auction_address=None,
            token_address=None,
            limit=None,
            eligible_count=1,
            selected_count=1,
            ready_count=1,
            ranked_candidates=[candidate],
            skipped_during_prepare=[
                SkippedPreparedCandidate(
                    candidate=candidate,
                    reason="quote API failed: upstream timeout",
                    result=KickResult(
                        kick_tx_id=0,
                        status=KickStatus.ERROR,
                        error_message="quote API failed: upstream timeout",
                    ),
                )
            ],
        )
    )
    executor = MagicMock()

    def _persist_fail(run_id, candidate, now_iso, *, status, error_message, **kwargs):  # noqa: ANN001
        kick_tx_id = kick_tx_repo.insert(
            {
                "run_id": run_id,
                "operation_type": "kick",
                "source_type": candidate.source_type,
                "source_address": candidate.source_address,
                "strategy_address": candidate.source_address,
                "token_address": candidate.token_address,
                "auction_address": candidate.auction_address,
                "status": status.value if isinstance(status, KickStatus) else status,
                "created_at": now_iso,
                "error_message": error_message,
                "price_usd": candidate.price_usd,
                "want_address": candidate.want_address,
                "usd_value": kwargs.get("usd_value"),
            }
        )
        return KickResult(kick_tx_id=kick_tx_id, status=status, error_message=error_message)

    executor._fail = MagicMock(side_effect=_persist_fail)

    service = TxnService(
        session=session,
        kicker=MagicMock(),
        executor=executor,
        planner=planner,
        txn_run_repository=txn_run_repo,
        kick_tx_repository=kick_tx_repo,
        usd_threshold=100.0,
        max_data_age_seconds=600,
        cooldown_policy=CooldownPolicy(default_minutes=60, auction_token_overrides_minutes={}),
        ignore_policy=IgnorePolicy(
            ignored_sources=frozenset(),
            ignored_auctions=frozenset(),
            ignored_auction_tokens=frozenset(),
        ),
        lock_path=Path("/tmp/test_txn_daemon.lock"),
    )

    result = await service.run_once(live=True)

    assert result.status == "FAILED"
    assert result.kicks_attempted == 1
    assert result.kicks_failed == 1
    executor._fail.assert_called_once()
    rows = session.execute(select(models.kick_txs)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "ERROR"
    assert rows[0]["error_message"] == "quote API failed: upstream timeout"


@pytest.mark.asyncio
async def test_live_confirm_declined_continues_to_next_candidate(session):
    """Declining one candidate should not stop later candidates in non-batch mode."""
    _seed_candidate(session, strategy_address="0xstrategy1", token_address="0xtoken1", auction_address="0xauction1")
    _seed_candidate(session, strategy_address="0xstrategy2", token_address="0xtoken2", auction_address="0xauction2")

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id, inspection=None: _make_prepared_kick(c))
    kicker.execute_single = AsyncMock(side_effect=[
        KickResult(
            kick_tx_id=1,
            status=KickStatus.USER_SKIPPED,
            error_message="user declined confirmation",
        ),
        KickResult(
            kick_tx_id=2,
            status=KickStatus.CONFIRMED,
            tx_hash="0xdef",
            gas_used=180000,
            block_number=12346,
        ),
    ])

    service = _build_txn_service(session, kicker=kicker)
    result = await service.run_once(live=True, batch=False)

    assert result.status == "SUCCESS"
    assert result.candidates_found == 2
    assert result.kicks_attempted == 1
    assert result.kicks_succeeded == 1
    assert result.kicks_failed == 0
    assert kicker.execute_single.await_count == 2


@pytest.mark.asyncio
async def test_submitted_blocks_resend(session):
    """A SUBMITTED kick_txs row should block re-sending for same pair."""
    _seed_candidate(session)

    # First run: kick returns SUBMITTED (receipt timeout).
    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=lambda c, run_id, inspection=None: _make_prepared_kick(c))
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
        "source_type": "strategy",
        "source_address": "0xstrategy1",
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


@pytest.mark.asyncio
async def test_live_batch_orders_candidates_by_descending_usd_value(session):
    _seed_candidate(
        session,
        strategy_address="0xstrategy1",
        token_address="0xtoken1",
        auction_address="0xauction1",
        want_address="0xwant1",
        price_usd="2.0",
        normalized_balance="100",
    )
    _seed_candidate(
        session,
        strategy_address="0xstrategy2",
        token_address="0xtoken2",
        auction_address="0xauction2",
        want_address="0xwant2",
        price_usd="3.0",
        normalized_balance="200",
    )

    prepare_order: list[str] = []
    executed_order: list[str] = []

    async def prepare_side_effect(candidate, run_id, inspection=None):
        prepare_order.append(candidate.token_address)
        return _make_prepared_kick(candidate)

    async def execute_batch_side_effect(prepared_kicks, run_id):
        executed_order.extend(pk.candidate.token_address for pk in prepared_kicks)
        return [
            KickResult(
                kick_tx_id=index + 1,
                status=KickStatus.CONFIRMED,
                tx_hash=f"0xabc{index}",
            )
            for index, _ in enumerate(prepared_kicks)
        ]

    kicker = MagicMock()
    kicker.prepare_kick = AsyncMock(side_effect=prepare_side_effect)
    kicker.execute_batch = AsyncMock(side_effect=execute_batch_side_effect)

    service = _build_txn_service(session, kicker=kicker)
    with patch("tidal.transaction_service.service.logger.info") as log_info:
        result = await service.run_once(live=True)

    assert result.status == "SUCCESS"
    assert prepare_order == ["0xtoken2", "0xtoken1"]
    assert executed_order == ["0xtoken2", "0xtoken1"]

    ranked_call = next(
        call
        for call in log_info.call_args_list
        if call.args and call.args[0] == "txn_candidates_ranked"
    )
    assert [entry["token"] for entry in ranked_call.kwargs["candidates"]] == ["0xtoken2", "0xtoken1"]


@pytest.mark.asyncio
async def test_dry_run_filters_to_fee_burner_candidates(session):
    _seed_candidate(session)
    _seed_fee_burner_candidate(session)

    service = _build_txn_service(session)
    result = await service.run_once(live=False, source_type="fee_burner")

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 1
    assert result.kicks_attempted == 1

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["source_type"] == "fee_burner"
    assert kick_txs[0]["source_address"] == "0xburner1"
    assert kick_txs[0]["strategy_address"] is None


@pytest.mark.asyncio
async def test_dry_run_filters_to_specific_source_address(session):
    source_a = _evm_address("1")
    source_b = _evm_address("2")
    auction_a = _evm_address("a")
    auction_b = _evm_address("b")

    _seed_candidate(
        session,
        strategy_address=source_a,
        token_address="0xtoken1",
        auction_address=auction_a,
        want_address="0xwant1",
    )
    _seed_candidate(
        session,
        strategy_address=source_b,
        token_address="0xtoken2",
        auction_address=auction_b,
        want_address="0xwant2",
    )

    service = _build_txn_service(session)
    result = await service.run_once(live=False, source_address=source_a)

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 1
    assert result.kicks_attempted == 1

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["source_address"] == source_a
    assert kick_txs[0]["auction_address"] == auction_a


@pytest.mark.asyncio
async def test_dry_run_filters_to_specific_auction_before_dedupe(session):
    burner_address = _evm_address("3")
    target_auction = _evm_address("c")
    other_auction = _evm_address("d")

    _seed_fee_burner_candidate(
        session,
        burner_address=burner_address,
        token_address="0xtokenfb1",
        auction_address=target_auction,
        want_address="0xwantfb",
        price_usd="10.0",
        normalized_balance="50",
    )
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.tokens).values(
        address="0xtokenfb2",
        chain_id=1,
        decimals=18,
        is_core_reward=0,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
    ))
    session.execute(insert(models.fee_burner_token_balances_latest).values(
        fee_burner_address=burner_address,
        token_address="0xtokenfb2",
        raw_balance="200000000000000000000",
        normalized_balance="200",
        block_number=101,
        scanned_at=now,
    ))
    _seed_fee_burner_candidate(
        session,
        burner_address=_evm_address("4"),
        token_address="0xtokenfb3",
        auction_address=other_auction,
        want_address="0xwantfc",
        price_usd="5.0",
        normalized_balance="30",
    )

    service = _build_txn_service(session)
    result = await service.run_once(live=False, auction_address=target_auction)

    assert result.status == "DRY_RUN"
    assert result.eligible_candidates_found == 2
    assert result.candidates_found == 1
    assert result.deferred_same_auction_count == 1
    assert result.kicks_attempted == 1

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["auction_address"] == target_auction
    assert kick_txs[0]["token_address"] == "0xtokenfb1"


@pytest.mark.asyncio
async def test_dry_run_combines_type_and_address_filters(session):
    shared_source = _evm_address("5")
    _seed_candidate(
        session,
        strategy_address=shared_source,
        token_address="0xtoken1",
        auction_address=_evm_address("e"),
        want_address="0xwant1",
    )
    _seed_fee_burner_candidate(
        session,
        burner_address=shared_source,
        token_address="0xtokenfb",
        auction_address=_evm_address("f"),
        want_address="0xwantfb",
    )

    service = _build_txn_service(session)
    result = await service.run_once(live=False, source_type="fee_burner", source_address=shared_source)

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 1
    assert result.kicks_attempted == 1

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["source_type"] == "fee_burner"
    assert kick_txs[0]["source_address"] == shared_source


@pytest.mark.asyncio
async def test_dry_run_target_filter_with_no_match_returns_zero_candidates(session):
    _seed_candidate(
        session,
        strategy_address=_evm_address("6"),
        token_address="0xtoken1",
        auction_address=_evm_address("7"),
        want_address="0xwant1",
    )

    service = _build_txn_service(session)
    result = await service.run_once(live=False, source_address=_evm_address("8"))

    assert result.status == "DRY_RUN"
    assert result.candidates_found == 0
    assert result.kicks_attempted == 0

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 0


@pytest.mark.asyncio
async def test_dry_run_keeps_one_candidate_per_auction(session):
    _seed_fee_burner_candidate(
        session,
        burner_address="0xburner1",
        token_address="0xtokenfb1",
        auction_address="0xauctionfb",
        want_address="0xwantfb",
        price_usd="10.0",
        normalized_balance="50",
    )
    now = datetime.now(timezone.utc).isoformat()
    session.execute(insert(models.tokens).values(
        address="0xtokenfb2",
        chain_id=1,
        decimals=18,
        is_core_reward=0,
        price_usd="2.0",
        price_status="SUCCESS",
        price_fetched_at=now,
        first_seen_at=now,
        last_seen_at=now,
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

    service = _build_txn_service(session)
    result = await service.run_once(live=False, source_type="fee_burner")

    assert result.status == "DRY_RUN"
    assert result.eligible_candidates_found == 2
    assert result.candidates_found == 1
    assert result.deferred_same_auction_count == 1
    assert result.kicks_attempted == 1

    kick_txs = session.execute(select(models.kick_txs)).mappings().all()
    assert len(kick_txs) == 1
    assert kick_txs[0]["source_type"] == "fee_burner"
    assert kick_txs[0]["token_address"] == "0xtokenfb1"
