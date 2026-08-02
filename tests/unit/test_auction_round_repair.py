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
        settings=SimpleNamespace(auction_kicker_address=KICKER),
        web3_client=web3,
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

    async def get_receipt(tx_hash: str, *, timeout_seconds: int):
        del timeout_seconds
        return receipts[tx_hash]

    web3 = SimpleNamespace(
        get_transaction_receipt=AsyncMock(side_effect=get_receipt),
        get_block=AsyncMock(return_value={"timestamp": 1_754_131_200}),
    )

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
    assert first_rows == second_rows
    assert len(second_rows) == 2
    assert second_rows[0]["requested_sell_amount"] == "100"
    assert second_rows[0]["sell_amount"] == "100"
    assert second_rows[1]["sell_amount"] == "0"
    assert second_rows[1]["round_kick_id"] == second_rows[0]["id"]
