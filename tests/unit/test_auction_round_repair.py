from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.auction_round_repair import AuctionRoundRepair
from tidal.operation_reconciler import DecodedKick, DecodedReceipt, DecodedResolve
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.persistence.repositories import KickTxRepository
from tidal.transaction_service.kick_policy import IgnorePolicy


AUCTION = "0x00000000000000000000000000000000000000a1"
TOKEN = "0x00000000000000000000000000000000000000b1"
SOURCE = "0x00000000000000000000000000000000000000c1"
KICKER = "0x00000000000000000000000000000000000000d1"
MINED_AT = datetime.fromtimestamp(1_754_131_200, tz=timezone.utc).isoformat()


@pytest.fixture
def session(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'repair.db'}")
    models.metadata.create_all(database.engine)
    session = database.session()
    session.execute(
        models.strategies.insert().values(
            address=SOURCE,
            chain_id=1,
            vault_address="0xvault",
            active=1,
            auction_address=AUCTION,
            want_address="0x00000000000000000000000000000000000000e1",
            first_seen_at=MINED_AT,
            last_seen_at=MINED_AT,
        )
    )
    session.execute(
        models.tokens.insert().values(
            address=TOKEN,
            chain_id=1,
            decimals=18,
            price_usd="1",
            price_status="SUCCESS",
            price_fetched_at=MINED_AT,
            first_seen_at=MINED_AT,
            last_seen_at=MINED_AT,
        )
    )
    session.execute(
        models.strategy_token_balances_latest.insert().values(
            strategy_address=SOURCE,
            token_address=TOKEN,
            raw_balance="100",
            normalized_balance="100",
            block_number=100,
            scanned_at=MINED_AT,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        database.engine.dispose()


def _row(operation_type: str, tx_hash: str, *, created_at: str, **values):
    return {
        "run_id": "repair-test",
        "operation_type": operation_type,
        "source_type": "strategy",
        "source_address": SOURCE,
        "strategy_address": SOURCE,
        "token_address": TOKEN,
        "auction_address": AUCTION,
        "status": "SUBMITTED",
        "tx_hash": tx_hash,
        "created_at": created_at,
        **values,
    }


def _repair(session, web3):
    return AuctionRoundRepair(
        session=session,
        settings=SimpleNamespace(
            auction_kicker_address=KICKER,
            txn_usd_threshold=1,
            txn_data_freshness_limit_seconds=999_999_999,
            kick_config=SimpleNamespace(
                ignore_policy=IgnorePolicy(
                    frozenset(),
                    frozenset(),
                    frozenset(),
                )
            ),
        ),
        web3_client=web3,
    )


def _web3(receipts, *, active: bool = False, latest_block: int = 101):  # noqa: ANN001
    async def get_receipt(tx_hash: str, *, timeout_seconds: int):
        del timeout_seconds
        return receipts[tx_hash]

    settlement_event = SimpleNamespace(get_logs=AsyncMock(return_value=[]))
    functions = SimpleNamespace(isActive=lambda token: ("isActive", token))
    return SimpleNamespace(
        get_transaction_receipt=AsyncMock(side_effect=get_receipt),
        get_block=AsyncMock(return_value={"timestamp": 1_754_131_200}),
        get_block_number=AsyncMock(return_value=latest_block),
        contract=lambda address, abi: SimpleNamespace(  # noqa: ARG005
            events=SimpleNamespace(AuctionSettled=lambda: settlement_event),
            functions=functions,
        ),
        call=AsyncMock(return_value=active),
    )


@pytest.mark.asyncio
async def test_repair_check_is_read_only_and_fails_ambiguous_evidence(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            "kick",
            "0xkick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="90",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    repo.insert(
        _row(
            "resolve_auction",
            "0xresolve",
            created_at=MINED_AT,
            status="CONFIRMED",
            sell_amount="90",
            round_kick_id=kick_id,
            resolution_path=1,
            block_number=101,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    before = repo.list_pair_operations(AUCTION, TOKEN)
    report = await _repair(session, SimpleNamespace()).run(apply=False)
    after = repo.list_pair_operations(AUCTION, TOKEN)
    assert report.passed is False
    assert report.pairs[0].outcome == "UNKNOWN"
    assert after == before


@pytest.mark.asyncio
async def test_repair_apply_is_idempotent_and_following_check_passes(session) -> None:
    repo = KickTxRepository(session)
    repo.insert(_row("kick", "0xkick", created_at="2026-08-02T12:00:00+00:00"))
    repo.insert(
        _row(
            "resolve_auction",
            "0xresolve",
            created_at="2026-08-02T12:01:00+00:00",
        )
    )
    receipts = {
        "0xkick": {
            "kind": "kick",
            "status": 1,
            "blockNumber": 100,
            "transactionIndex": 0,
            "gasUsed": 100,
            "effectiveGasPrice": 1_000_000_000,
            "logs": [],
        },
        "0xresolve": {
            "kind": "resolve",
            "status": 1,
            "blockNumber": 101,
            "transactionIndex": 0,
            "gasUsed": 100,
            "effectiveGasPrice": 1_000_000_000,
            "logs": [],
        },
    }

    web3 = _web3(receipts)

    def decode(receipt, auctions):  # noqa: ANN001
        assert auctions == [AUCTION] or auctions == (AUCTION,)
        if receipt["kind"] == "kick":
            return DecodedReceipt(
                kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 100),)
            )
        return DecodedReceipt(resolves=(DecodedResolve(AUCTION, TOKEN, 1, 0),))

    repair = _repair(session, web3)
    repair.reconciler.decode_receipt_fn = decode
    first = await repair.run(apply=True)
    first_rows = repo.list_pair_operations(AUCTION, TOKEN)
    second = await repair.run(apply=True)
    second_rows = repo.list_pair_operations(AUCTION, TOKEN)
    check = await repair.run(apply=False)

    assert first.passed and second.passed and check.passed
    assert first.mutations == 2
    assert second.mutations == 0
    assert check.mutations == 0
    assert first_rows == second_rows
    assert len(second_rows) == 2
    assert second_rows[0]["requested_sell_amount"] == "100"
    assert second_rows[0]["sell_amount"] == "100"
    assert second_rows[1]["sell_amount"] == "0"
    assert second_rows[1]["round_kick_id"] == second_rows[0]["id"]


@pytest.mark.asyncio
async def test_repair_check_covers_historical_pair_without_current_candidate(
    session,
) -> None:
    repo = KickTxRepository(session)
    repo.insert(
        _row(
            "kick",
            "0xkick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="90",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    session.execute(
        models.strategy_token_balances_latest.update()
        .where(
            models.strategy_token_balances_latest.c.strategy_address == SOURCE,
            models.strategy_token_balances_latest.c.token_address == TOKEN,
        )
        .values(raw_balance="0", normalized_balance="0")
    )
    session.commit()

    report = await _repair(session, SimpleNamespace()).run(apply=False)

    assert report.passed is False
    assert report.pairs[0].outcome == "UNKNOWN"


@pytest.mark.asyncio
async def test_unreviewed_old_submitted_row_fails_full_history_audit(session) -> None:
    repo = KickTxRepository(session)
    repo.insert(_row("kick", "0xold", created_at="2026-08-02T10:00:00+00:00"))
    kick_id = repo.insert(
        _row(
            "kick",
            "0xnew",
            created_at="2026-08-02T12:00:00+00:00",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=0,
            mined_at="2026-08-02T12:00:00+00:00",
        )
    )
    repo.insert(
        _row(
            "resolve_auction",
            "0xresolve",
            created_at="2026-08-02T13:00:00+00:00",
            status="CONFIRMED",
            sell_amount="50",
            round_kick_id=kick_id,
            resolution_path=1,
            block_number=101,
            transaction_index=0,
            mined_at="2026-08-02T13:00:00+00:00",
        )
    )

    report = await _repair(session, SimpleNamespace()).run(apply=False)

    assert report.passed is False
    assert report.pairs[0].outcome == "PRODUCTIVE"


@pytest.mark.asyncio
async def test_repair_apply_baselines_inactive_unprovable_history(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            "kick",
            "0xkick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="90",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    repo.insert(
        _row(
            "resolve_auction",
            "0xresolve",
            created_at=MINED_AT,
            status="CONFIRMED",
            sell_amount="90",
            round_kick_id=kick_id,
            resolution_path=1,
            block_number=101,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    receipts = {
        "0xkick": {
            "kind": "kick",
            "status": 1,
            "blockNumber": 100,
            "transactionIndex": 0,
            "gasUsed": 100,
            "effectiveGasPrice": 1_000_000_000,
            "logs": [],
        },
        "0xresolve": {
            "kind": "resolve",
            "status": 1,
            "blockNumber": 101,
            "transactionIndex": 0,
            "gasUsed": 100,
            "effectiveGasPrice": 1_000_000_000,
            "logs": [],
        },
    }
    web3 = _web3(receipts, active=False)
    repair = _repair(session, web3)
    repair.reconciler.decode_receipt_fn = lambda receipt, auctions: (
        DecodedReceipt(kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 90),))
        if receipt["kind"] == "kick"
        else DecodedReceipt(resolves=(DecodedResolve(AUCTION, TOKEN, 1, 90),))
    )

    first = await repair.run(apply=True)
    second = await repair.run(apply=True)

    row = repo.get(kick_id)
    assert row is not None
    assert row["historical_baseline"] == 1
    assert row["historical_baseline_reason"] == "REQUESTED_PLACED_MISMATCH"
    assert first.passed and second.passed
    assert first.pairs[0].outcome == "HISTORICAL_BASELINE"
    assert first.pairs[0].baseline_kick_ids == (kick_id,)
    assert second.mutations == 0


@pytest.mark.asyncio
async def test_repair_does_not_baseline_active_unprovable_round(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            "kick",
            "0xkick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="90",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    receipts = {
        "0xkick": {
            "status": 1,
            "blockNumber": 100,
            "transactionIndex": 0,
            "gasUsed": 100,
            "effectiveGasPrice": 1_000_000_000,
            "logs": [],
        }
    }
    web3 = _web3(receipts, active=True)
    repair = _repair(session, web3)
    repair.reconciler.decode_receipt_fn = lambda receipt, auctions: DecodedReceipt(
        kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 90),)
    )

    report = await repair.run(apply=True)

    row = repo.get(kick_id)
    assert row is not None
    assert row["historical_baseline"] == 0
    assert report.passed is False


def test_repair_recomputes_round_links_in_chain_order(session) -> None:
    repo = KickTxRepository(session)
    old_kick_id = repo.insert(
        _row(
            "kick",
            "0xold-kick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    old_close_id = repo.insert(
        _row(
            "resolve_auction",
            "0xold-close",
            created_at=MINED_AT,
            status="CONFIRMED",
            sell_amount="100",
            resolution_path=5,
            block_number=101,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    latest_kick_id = repo.insert(
        _row(
            "kick",
            "0xlatest-kick",
            created_at=MINED_AT,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=200,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    latest_close_id = repo.insert(
        _row(
            "resolve_auction",
            "0xlatest-close",
            created_at=MINED_AT,
            status="CONFIRMED",
            sell_amount="50",
            resolution_path=5,
            round_kick_id=old_kick_id,
            block_number=201,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    no_op_id = repo.insert(
        _row(
            "resolve_auction",
            "0xnoop",
            created_at=MINED_AT,
            status="CONFIRMED",
            sell_amount="0",
            resolution_path=0,
            round_kick_id=latest_kick_id,
            block_number=201,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )

    _repair(session, SimpleNamespace()).reconciler.rebuild_round_links(
        {(AUCTION, TOKEN)}
    )

    rows = {int(row["id"]): row for row in repo.list_pair_operations(AUCTION, TOKEN)}
    assert rows[old_close_id]["round_kick_id"] == old_kick_id
    assert rows[latest_close_id]["round_kick_id"] == latest_kick_id
    assert rows[no_op_id]["round_kick_id"] is None
