from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from tidal.operation_reconciler import (
    DecodedKick,
    DecodedReceipt,
    DecodedResolve,
    DecodedSettlement,
    DecodedSweep,
    OperationReconciler,
)
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.persistence.repositories import KickTxRepository


AUCTION = "0x00000000000000000000000000000000000000a1"
TOKEN = "0x00000000000000000000000000000000000000b1"
SOURCE = "0x00000000000000000000000000000000000000c1"
KICKER = "0x00000000000000000000000000000000000000d1"
HISTORICAL_KICKER = "0x00000000000000000000000000000000000000d2"
MINED_AT = datetime.fromtimestamp(1_754_131_200, tz=timezone.utc).isoformat()


@pytest.fixture
def session(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'reconciler.db'}")
    models.metadata.create_all(database.engine)
    session = database.session()
    session.execute(
        insert(models.tokens).values(
            address=TOKEN,
            chain_id=1,
            symbol="TKN",
            decimals=18,
            first_seen_at=MINED_AT,
            last_seen_at=MINED_AT,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        database.engine.dispose()


def _row(*, operation_type: str, tx_hash: str, status: str = "SUBMITTED", **values):
    return {
        "run_id": "test-run",
        "operation_type": operation_type,
        "source_type": "strategy",
        "source_address": SOURCE,
        "strategy_address": SOURCE,
        "token_address": TOKEN,
        "auction_address": AUCTION,
        "status": status,
        "tx_hash": tx_hash,
        "created_at": MINED_AT,
        **values,
    }


def _web3(receipts: dict[str, dict[str, object]] | None = None):
    receipts = receipts or {}

    async def get_receipt(tx_hash: str, *, timeout_seconds: int):
        del timeout_seconds
        return receipts[tx_hash]

    return SimpleNamespace(
        get_transaction_receipt=AsyncMock(side_effect=get_receipt),
        get_block=AsyncMock(return_value={"timestamp": 1_754_131_200}),
    )


def _receipt(*, block: int = 100, transaction_index: int = 2):
    return {
        "status": 1,
        "blockNumber": block,
        "transactionIndex": transaction_index,
        "gasUsed": 123_456,
        "effectiveGasPrice": 2_000_000_000,
        "logs": [],
    }


class _SettlementEvent:
    def __init__(self, logs):
        self.logs = logs

    async def get_logs(self, **kwargs):  # noqa: ANN003
        assert kwargs["argument_filters"]["from"].lower() == TOKEN
        return self.logs


class _SettlementEvents:
    def __init__(self, logs):
        self.logs = logs

    def AuctionSettled(self):
        return _SettlementEvent(self.logs)


@pytest.mark.asyncio
async def test_kick_uses_actual_placed_amount_and_batch_receipt_is_fetched_once(
    session,
) -> None:
    repo = KickTxRepository(session)
    repo.insert(
        _row(operation_type="kick", tx_hash="0xabc", requested_sell_amount="999")
    )
    repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xabc",
            token_address=TOKEN,
            requested_sell_amount="888",
        )
    )
    web3 = _web3({"0xabc": _receipt()})
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 90),),
        ),
    )

    assert await reconciler.reconcile_submitted() == []
    rows = repo.list_by_tx_hash("0xabc")
    assert len(rows) == 2
    assert all(row["status"] == "CONFIRMED" for row in rows)
    assert all(row["requested_sell_amount"] == "100" for row in rows)
    assert all(row["sell_amount"] == "90" for row in rows)
    assert all(row["normalized_balance"] == "0.00000000000000009" for row in rows)
    assert all(
        row["transaction_index"] == 2 and row["mined_at"] == MINED_AT for row in rows
    )
    web3.get_transaction_receipt.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_recovery_resolve_is_canonical_and_linked(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    resolve_id = repo.insert(
        _row(operation_type="resolve_auction", tx_hash="0xresolve")
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=_web3(),
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            resolves=(DecodedResolve(AUCTION, TOKEN, 1, 0),),
        ),
    )

    assert await reconciler.finalize_receipt("0xresolve", _receipt(block=101)) is None
    row = repo.get(resolve_id)
    assert row is not None
    assert row["sell_amount"] == "0"
    assert row["normalized_balance"] == "0"
    assert row["resolution_path"] == 1
    assert row["round_kick_id"] == kick_id


@pytest.mark.asyncio
async def test_resolve_and_settlement_in_same_receipt_create_one_close(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    repo.insert(
        _row(
            operation_type="resolve_auction", tx_hash="0xresolve", round_kick_id=kick_id
        )
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=_web3(),
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            resolves=(DecodedResolve(AUCTION, TOKEN, 3, 40),),
            sweeps=(DecodedSweep(AUCTION, TOKEN, 40),),
            settlements=(DecodedSettlement(AUCTION, TOKEN),),
        ),
    )

    await reconciler.finalize_receipt("0xresolve", _receipt(block=101))
    pair_rows = repo.list_pair_operations(AUCTION, TOKEN)
    assert [row["operation_type"] for row in pair_rows].count("auction_settled") == 0


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent(session) -> None:
    repo = KickTxRepository(session)
    repo.insert(_row(operation_type="kick", tx_hash="0xabc"))
    reconciler = OperationReconciler(
        session=session,
        web3_client=_web3(),
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 100),),
        ),
    )
    await reconciler.finalize_receipt("0xabc", _receipt())
    await reconciler.finalize_receipt("0xabc", _receipt())
    assert len(repo.list_by_tx_hash("0xabc")) == 1


@pytest.mark.asyncio
async def test_direct_settlement_discovery_is_linked_and_idempotent(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    web3 = _web3({"0xsettle": _receipt(block=101, transaction_index=3)})
    web3.contract = lambda address, abi: SimpleNamespace(  # noqa: ARG005
        events=_SettlementEvents(
            [{"blockNumber": 101, "transactionIndex": 3, "transactionHash": "0xsettle"}]
        )
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            settlements=(DecodedSettlement(AUCTION, TOKEN),),
        ),
    )

    assert await reconciler.discover_direct_settlements() == []
    assert await reconciler.discover_direct_settlements() == []
    rows = repo.list_pair_operations(AUCTION, TOKEN)
    settlements = [row for row in rows if row["operation_type"] == "auction_settled"]
    assert len(settlements) == 1
    assert settlements[0]["round_kick_id"] == kick_id
    assert settlements[0]["sell_amount"] == "0"


@pytest.mark.asyncio
async def test_direct_settlement_discovery_ignores_older_unclosed_rounds(
    session,
) -> None:
    repo = KickTxRepository(session)
    repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xold",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    latest_kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xlatest",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=200,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    repo.insert(
        _row(
            operation_type="resolve_auction",
            tx_hash="0xresolve",
            status="CONFIRMED",
            sell_amount="50",
            resolution_path=1,
            round_kick_id=latest_kick_id,
            block_number=201,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    web3 = _web3()

    def unexpected_contract(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        pytest.fail("historical round should not trigger an event lookup")

    web3.contract = unexpected_contract
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
    )

    assert await reconciler.discover_direct_settlements() == []


def test_noop_resolution_does_not_mark_kick_closed(session) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=100,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )
    repo.insert(
        _row(
            operation_type="resolve_auction",
            tx_hash="0xnoop",
            status="CONFIRMED",
            sell_amount="0",
            resolution_path=0,
            round_kick_id=kick_id,
            block_number=101,
            transaction_index=0,
            mined_at=MINED_AT,
        )
    )

    open_kick = repo.latest_confirmed_unclosed_kick(AUCTION, TOKEN)

    assert open_kick is not None
    assert open_kick["id"] == kick_id


def test_foreign_key_enforcement_rejects_invalid_round_link(session) -> None:
    with pytest.raises(IntegrityError):
        KickTxRepository(session).insert(
            _row(
                operation_type="resolve_auction",
                tx_hash="0xbad",
                round_kick_id=999,
            )
        )
    session.rollback()


def test_receipt_destination_selects_the_historical_kicker_contract(session) -> None:
    captured_addresses: list[str] = []

    class EmptyEvent:
        def process_receipt(self, receipt, *, errors):  # noqa: ANN001
            del receipt, errors
            return []

    class KickerEvents:
        Kicked = AuctionResolved = AuctionSwept = EmptyEvent

    web3 = _web3()

    def contract(address, abi):  # noqa: ANN001
        del abi
        captured_addresses.append(address.lower())
        return SimpleNamespace(events=KickerEvents())

    web3.contract = contract
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
    )

    decoded = reconciler._decode_receipt({"to": HISTORICAL_KICKER, "logs": []}, ())

    assert decoded == DecodedReceipt()
    assert captured_addresses == [HISTORICAL_KICKER]
