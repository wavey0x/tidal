from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode
from hexbytes import HexBytes
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from web3 import Web3

from tidal.constants import TRUSTED_HISTORICAL_AUCTION_KICKERS
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
TOKEN_2 = "0x00000000000000000000000000000000000000b2"
SOURCE = "0x00000000000000000000000000000000000000c1"
KICKER = "0x00000000000000000000000000000000000000d1"
HISTORICAL_KICKER = "0x2a76c6ad151af2edbe16755fc3bff67176f01071"
MINED_AT = datetime.fromtimestamp(1_754_131_200, tz=timezone.utc).isoformat()


@pytest.fixture
def session(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'reconciler.db'}", create=True)
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


def _web3(
    receipts: dict[str, dict[str, object]] | None = None,
    *,
    latest_block: int = 101,
):
    receipts = receipts or {}

    async def get_receipt(tx_hash: str, *, timeout_seconds: int):
        del timeout_seconds
        return {**receipts[tx_hash], "transactionHash": tx_hash, "from": SOURCE, "to": AUCTION}

    async def get_transaction(tx_hash):
        receipt = receipts[tx_hash]
        return {"hash": tx_hash, "chainId": 1, "from": SOURCE, "nonce": 7,
                "to": AUCTION, "input": "0x1234", "value": 0,
                "blockNumber": receipt["blockNumber"], "blockHash": receipt["blockHash"],
                "transactionIndex": receipt["transactionIndex"]}

    async def get_block(identifier):
        return {"number": latest_block if identifier == "finalized" else identifier,
                "hash": "0x" + "22" * 32, "timestamp": 1_754_131_200}

    return SimpleNamespace(
        get_transaction_receipt=AsyncMock(side_effect=get_receipt),
        get_transaction=AsyncMock(side_effect=get_transaction),
        get_chain_id=AsyncMock(return_value=1),
        get_block=AsyncMock(side_effect=get_block),
        get_block_number=AsyncMock(return_value=latest_block),
    )


def _receipt(*, block: int = 100, transaction_index: int = 2):
    return {
        "status": 1,
        "blockNumber": block,
        "blockHash": "0x" + "22" * 32,
        "transactionIndex": transaction_index,
        "gasUsed": 123_456,
        "effectiveGasPrice": 2_000_000_000,
        "logs": [],
    }


def _apply_verified_receipt(reconciler, tx_hash, receipt):
    """Business decoding tests; native proof/atomicity is tested independently."""
    return reconciler._finalize_operations(tx_hash, receipt,
        reconciler.kick_repo.list_by_tx_hash(tx_hash), {"timestamp": 1_754_131_200})


def _event_log(
    *,
    address: str,
    signature: str,
    indexed_addresses: tuple[str, ...],
    data_types: tuple[str, ...],
    data_values: tuple[object, ...],
    log_index: int,
) -> dict[str, object]:
    return {
        "address": address,
        "topics": [
            Web3.keccak(text=signature),
            *(
                HexBytes(b"\0" * 12 + bytes.fromhex(indexed[2:]))
                for indexed in indexed_addresses
            ),
        ],
        "data": HexBytes(encode(data_types, data_values)),
        "blockNumber": 100,
        "transactionHash": HexBytes(b"\x11" * 32),
        "transactionIndex": 2,
        "blockHash": HexBytes(b"\x22" * 32),
        "logIndex": log_index,
        "removed": False,
    }


class _SettlementEvent:
    def __init__(self, logs):
        self.logs = logs
        self.ranges = []
        self.filters = []

    async def get_logs(self, **kwargs):  # noqa: ANN003
        block_range = (kwargs["from_block"], kwargs["to_block"])
        self.ranges.append(block_range)
        self.filters.append(kwargs.get("argument_filters"))
        return [
            log
            for log in self.logs
            if block_range[0] <= log["blockNumber"] <= block_range[1]
        ]


class _SettlementEvents:
    def __init__(self, logs):
        self.event = _SettlementEvent(logs)

    def AuctionSettled(self):
        return self.event


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["wrong_chain", "receipt_reorg", "missing_sender", "log_position", "not_finalized"])
async def test_direct_settlement_requires_matching_finalized_chain_evidence(session, defect):
    repo = KickTxRepository(session)
    repo.insert(_row(operation_type="kick", tx_hash="0x" + "66" * 32, status="CONFIRMED",
        requested_sell_amount="100", sell_amount="100", block_number=100,
        transaction_index=1, mined_at=MINED_AT))
    tx_hash = "0x" + "77" * 32
    web3 = _web3({tx_hash: _receipt(block=102)}, latest_block=101 if defect == "not_finalized" else 103)
    events = _SettlementEvents([{"blockNumber": 102, "transactionIndex": 3 if defect == "log_position" else 2,
                                "transactionHash": tx_hash}])
    web3.contract = lambda *_: SimpleNamespace(events=events)
    if defect == "wrong_chain":
        web3.get_chain_id.return_value = 2
    elif defect in {"receipt_reorg", "missing_sender"}:
        original = web3.get_transaction_receipt.side_effect
        async def defective(*args, **kwargs):
            receipt = await original(*args, **kwargs)
            if defect == "receipt_reorg":
                receipt["blockHash"] = "0x" + "99" * 32
            else:
                del receipt["from"]
            return receipt
        web3.get_transaction_receipt.side_effect = defective
    reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER,
        decode_receipt_fn=lambda *_: DecodedReceipt(settlements=(DecodedSettlement(AUCTION, TOKEN),)))
    errors = await reconciler.discover_direct_settlements()
    assert bool(errors) == (defect != "not_finalized")
    assert len(repo.list_pair_operations(AUCTION, TOKEN)) == 1
    if defect == "not_finalized":
        web3.get_transaction_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_round_uses_bounded_chunks_before_reporting_exhaustion(session):
    repo = KickTxRepository(session)
    repo.insert(_row(operation_type="kick", tx_hash="0x" + "66" * 32, status="CONFIRMED",
        requested_sell_amount="100", sell_amount="100", block_number=1,
        transaction_index=1, mined_at=MINED_AT))
    web3 = _web3(latest_block=1_000_001)
    events = _SettlementEvents([])
    web3.contract = lambda *_: SimpleNamespace(events=events)
    reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
    errors = await reconciler.discover_direct_settlements()
    assert [error.error_code for error in errors] == ["known_history_limit"]
    assert len(events.event.ranges) == 20
    assert events.event.ranges[0] == (1, 50_000)
    assert events.event.ranges[-1] == (950_001, 1_000_000)
    assert errors[0].kick_id is not None
    assert errors[0].auction_address == AUCTION
    assert errors[0].token_address == TOKEN


@pytest.mark.asyncio
async def test_kick_uses_actual_placed_amount_for_all_batch_operations(
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

    assert _apply_verified_receipt(reconciler, "0xabc", _receipt()) is None
    rows = repo.list_by_tx_hash("0xabc")
    assert len(rows) == 2
    assert all(row["status"] == "CONFIRMED" for row in rows)
    assert all(row["requested_sell_amount"] == "100" for row in rows)
    assert all(row["sell_amount"] == "90" for row in rows)
    assert all(row["normalized_balance"] == "0.00000000000000009" for row in rows)
    assert all(
        row["transaction_index"] == 2 and row["mined_at"] == MINED_AT for row in rows
    )
    web3.get_transaction_receipt.assert_not_awaited()


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

    assert _apply_verified_receipt(reconciler, "0xresolve", _receipt(block=101)) is None
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

    _apply_verified_receipt(reconciler, "0xresolve", _receipt(block=101))
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
    _apply_verified_receipt(reconciler, "0xabc", _receipt())
    _apply_verified_receipt(reconciler, "0xabc", _receipt())
    assert len(repo.list_by_tx_hash("0xabc")) == 1


@pytest.mark.asyncio
async def test_verified_event_corrects_requested_and_placed_ambiguity(
    session,
) -> None:
    repo = KickTxRepository(session)
    kick_id = repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick",
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="90",
            block_number=100,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    web3 = _web3({"0xkick": _receipt(block=100, transaction_index=1)})
    settlement_events = _SettlementEvents([])
    web3.contract = lambda address, abi: SimpleNamespace(  # noqa: ARG005
        events=settlement_events
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            kicks=(DecodedKick(SOURCE, AUCTION, TOKEN, 100, 100),),
        ),
    )

    assert _apply_verified_receipt(reconciler, "0xkick", _receipt(block=100, transaction_index=1)) is None

    row = repo.get(kick_id)
    assert row is not None
    assert row["sell_amount"] == "100"
    web3.get_transaction_receipt.assert_not_awaited()


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
    web3 = _web3(
        {"0x" + "33" * 32: _receipt(block=100_050, transaction_index=3)},
        latest_block=150_000,
    )
    settlement_events = _SettlementEvents(
        [
            {
                "blockNumber": 100_050,
                "transactionIndex": 3,
                "transactionHash": "0x" + "33" * 32,
            }
        ]
    )
    web3.contract = lambda address, abi: SimpleNamespace(  # noqa: ARG005
        events=settlement_events
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
    assert settlement_events.event.ranges == [
        (100, 50_099),
        (50_100, 100_099),
    ]
    web3.get_transaction_receipt.assert_awaited_once()


@pytest.mark.asyncio
async def test_settlement_logs_are_scoped_to_each_round_and_token(session) -> None:
    repo = KickTxRepository(session)
    repo.insert(
        _row(
            operation_type="kick",
            tx_hash="0xkick-1",
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
            operation_type="kick",
            tx_hash="0xkick-2",
            token_address=TOKEN_2,
            status="CONFIRMED",
            requested_sell_amount="100",
            sell_amount="100",
            block_number=101,
            transaction_index=1,
            mined_at=MINED_AT,
        )
    )
    web3 = _web3(latest_block=101)
    settlement_events = _SettlementEvents([])
    web3.contract = lambda address, abi: SimpleNamespace(  # noqa: ARG005
        events=settlement_events
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
    )

    assert await reconciler.discover_direct_settlements() == []
    assert settlement_events.event.ranges == [(101, 101), (100, 101)]
    assert settlement_events.event.filters == [{"from": Web3.to_checksum_address(TOKEN_2)}, {"from": Web3.to_checksum_address(TOKEN)}]


@pytest.mark.asyncio
async def test_direct_settlement_discovery_repairs_older_unclosed_rounds(
    session,
) -> None:
    repo = KickTxRepository(session)
    old_kick_id = repo.insert(
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
    web3 = _web3(
        {"0x" + "44" * 32: _receipt(block=150, transaction_index=3)},
        latest_block=250,
    )
    settlement_events = _SettlementEvents(
        [
            {
                "blockNumber": 150,
                "transactionIndex": 3,
                "transactionHash": "0x" + "44" * 32,
            }
        ]
    )
    web3.contract = lambda address, abi: SimpleNamespace(  # noqa: ARG005
        events=settlement_events
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
    settlements = [
        row
        for row in repo.list_pair_operations(AUCTION, TOKEN)
        if row["operation_type"] == "auction_settled"
    ]
    assert len(settlements) == 1
    assert settlements[0]["round_kick_id"] == old_kick_id


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


@pytest.mark.asyncio
async def test_noop_resolution_does_not_require_a_round_link(session) -> None:
    repo = KickTxRepository(session)
    repo.insert(_row(operation_type="resolve_auction", tx_hash="0xnoop"))
    reconciler = OperationReconciler(
        session=session,
        web3_client=_web3(),
        auction_kicker_address=KICKER,
        decode_receipt_fn=lambda receipt, auctions: DecodedReceipt(
            resolves=(DecodedResolve(AUCTION, TOKEN, 0, 0),),
        ),
    )

    error_code = _apply_verified_receipt(reconciler, "0xnoop", _receipt())

    row = repo.list_by_tx_hash("0xnoop")[0]
    assert error_code is None
    assert row["round_kick_id"] is None
    assert row["error_message"] is None


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


@pytest.mark.parametrize(
    ("signature", "data_types", "data_values"),
    [
        (
            "Kicked(address,address,address,uint256,uint256,uint256,uint256)",
            ("address", "uint256", "uint256", "uint256", "uint256"),
            (TOKEN, 100, 601, 200, 25),
        ),
        (
            "Kicked(address,address,address,uint256,uint256,uint256,uint256,address)",
            ("address", "uint256", "uint256", "uint256", "uint256", "address"),
            (TOKEN, 100, 601, 200, 25, KICKER),
        ),
        (
            "Kicked(address,address,address,uint256,uint256,uint256)",
            ("address", "uint256", "uint256", "uint256"),
            (TOKEN, 100, 601, 200),
        ),
    ],
)
def test_kicked_event_versions_restore_requested_and_placed_amounts(
    session,
    signature: str,
    data_types: tuple[str, ...],
    data_values: tuple[object, ...],
) -> None:
    web3 = _web3()
    decoder_web3 = Web3()
    web3.contract = lambda address, abi: decoder_web3.eth.contract(
        address=address, abi=abi
    )
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
    )
    receipt = {
        "to": HISTORICAL_KICKER,
        "logs": [
            _event_log(
                address=HISTORICAL_KICKER,
                signature=signature,
                indexed_addresses=(SOURCE, AUCTION),
                data_types=data_types,
                data_values=data_values,
                log_index=0,
            ),
            _event_log(
                address=AUCTION,
                signature="AuctionKicked(address,uint256)",
                indexed_addresses=(TOKEN,),
                data_types=("uint256",),
                data_values=(100,),
                log_index=1,
            ),
        ],
    }

    decoded = reconciler._decode_receipt(receipt, (AUCTION,))

    assert decoded.kicks == (DecodedKick(SOURCE, AUCTION, TOKEN, 100, 100),)


def test_receipt_destination_does_not_select_a_trusted_kicker(session) -> None:
    captured_addresses: list[str] = []

    class EmptyEvent:
        def process_receipt(self, receipt, *, errors):  # noqa: ANN001
            del receipt, errors
            return []

    class KickerEvents:
        AuctionResolved = AuctionSwept = EmptyEvent

    class KickerContract:
        events = KickerEvents()

        @staticmethod
        def get_event_by_signature(signature):  # noqa: ANN001
            del signature
            return EmptyEvent

    web3 = _web3()

    def contract(address, abi):  # noqa: ANN001
        del abi
        captured_addresses.append(address.lower())
        return KickerContract()

    web3.contract = contract
    reconciler = OperationReconciler(
        session=session,
        web3_client=web3,
        auction_kicker_address=KICKER,
    )

    decoded = reconciler._decode_receipt({"to": SOURCE, "logs": []}, ())

    assert decoded == DecodedReceipt()
    assert captured_addresses == [KICKER]


@pytest.mark.parametrize("kicker_address", [KICKER, *sorted(TRUSTED_HISTORICAL_AUCTION_KICKERS)])
def test_actual_decoder_keeps_multiple_auctions_separate(session, kicker_address):
    second_auction = "0x00000000000000000000000000000000000000a2"
    unrelated_auction = "0x00000000000000000000000000000000000000a3"
    decoder_web3 = Web3()
    web3 = _web3()
    web3.contract = lambda address, abi: decoder_web3.eth.contract(address=address, abi=abi)
    reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
    logs = []
    for auction, amount in [(AUCTION, 100), (second_auction, 200)]:
        logs.append(_event_log(
            address=kicker_address, signature="Kicked(address,address,address,uint256,uint256,uint256)",
            indexed_addresses=(SOURCE, auction), data_types=("address", "uint256", "uint256", "uint256"),
            data_values=(TOKEN, amount + 10, 601, 200), log_index=len(logs),
        ))
        logs.append(_event_log(
            address=auction, signature="AuctionKicked(address,uint256)", indexed_addresses=(TOKEN,),
            data_types=("uint256",), data_values=(amount,), log_index=len(logs),
        ))
    for auction in [second_auction, unrelated_auction]:
        logs.append(_event_log(
            address=auction, signature="AuctionSettled(address)", indexed_addresses=(TOKEN,),
            data_types=(), data_values=(), log_index=len(logs),
        ))
    receipt = {"to": SOURCE, "logs": logs}
    decoded = reconciler._decode_receipt(receipt, (AUCTION, second_auction))
    assert decoded.kicks == (
        DecodedKick(SOURCE, AUCTION, TOKEN, 110, 100),
        DecodedKick(SOURCE, second_auction, TOKEN, 210, 200),
    )
    assert decoded.settlements == (DecodedSettlement(second_auction, TOKEN),)
    assert receipt["logs"] == logs  # Filtering must not mutate a shared receipt.


def test_actual_decoder_ignores_untrusted_kicker_emitter_even_when_destination_matches(session):
    decoder_web3 = Web3()
    web3 = _web3()
    web3.contract = lambda address, abi: decoder_web3.eth.contract(address=address, abi=abi)
    reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
    receipt = {"to": SOURCE, "logs": [_event_log(
        address=SOURCE, signature="Kicked(address,address,address,uint256,uint256,uint256)",
        indexed_addresses=(SOURCE, AUCTION), data_types=("address", "uint256", "uint256", "uint256"),
        data_values=(TOKEN, 100, 601, 200), log_index=0,
    )]}
    assert reconciler._decode_receipt(receipt, (AUCTION,)) == DecodedReceipt()


@pytest.mark.parametrize("chain_id", [1, 2])
def test_actual_decoder_filters_resolve_and_sweep_emitters_on_their_trusted_chain(session, chain_id):
    decoder_web3 = Web3()
    web3 = _web3()
    web3.contract = lambda address, abi: decoder_web3.eth.contract(address=address, abi=abi)
    reconciler = OperationReconciler(
        session=session, web3_client=web3, auction_kicker_address=KICKER, chain_id=chain_id,
    )
    logs = []
    for address in [HISTORICAL_KICKER, SOURCE]:
        logs.append(_event_log(
            address=address, signature="AuctionResolved(address,address,uint8,address,uint256)",
            indexed_addresses=(AUCTION, TOKEN), data_types=("uint8", "address", "uint256"),
            data_values=(5, SOURCE, 100), log_index=len(logs),
        ))
        logs.append(_event_log(
            address=address, signature="AuctionSwept(address,address,address,uint256)",
            indexed_addresses=(AUCTION, TOKEN), data_types=("address", "uint256"),
            data_values=(SOURCE, 100), log_index=len(logs),
        ))
    decoded = reconciler._decode_receipt({"to": SOURCE, "logs": logs}, (AUCTION,))
    assert decoded.resolves == ((DecodedResolve(AUCTION, TOKEN, 5, 100),) if chain_id == 1 else ())
    assert decoded.sweeps == ((DecodedSweep(AUCTION, TOKEN, 100),) if chain_id == 1 else ())


def _confirmed_kick(repo, block, *, token=TOKEN, transaction_index=1):
    return repo.insert(_row(operation_type="kick", tx_hash="0x" + f"{block:064x}",
        status="CONFIRMED", token_address=token, requested_sell_amount="100", sell_amount="100",
        block_number=block, transaction_index=transaction_index, mined_at=MINED_AT))


def _settlement_reconciler(session, logs, receipts, *, head=10_000_000):
    web3 = _web3(receipts, latest_block=head)
    events = _SettlementEvents(logs)
    web3.contract = lambda *_: SimpleNamespace(events=events)
    reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER,
        decode_receipt_fn=lambda *_: DecodedReceipt(settlements=(DecodedSettlement(AUCTION, TOKEN),)))
    return reconciler, web3, events.event


@pytest.mark.asyncio
async def test_years_old_rounds_stop_in_their_first_chunks_and_stay_repaired(session):
    repo = KickTxRepository(session)
    old = _confirmed_kick(repo, 100)
    new = _confirmed_kick(repo, 2_000_000)
    hashes = ["0x" + "71" * 32, "0x" + "72" * 32]
    blocks = [102, 2_000_002]
    logs = [dict(blockNumber=b, transactionIndex=2, transactionHash=h) for b, h in zip(blocks, hashes)]
    reconciler, web3, events = _settlement_reconciler(session, logs, {h: _receipt(block=b) for b, h in zip(blocks, hashes)})
    assert await reconciler.discover_direct_settlements(max_log_chunks=1) == []
    assert events.ranges == [(2_000_000, 2_049_999), (100, 50_099)]
    closes = [r for r in repo.list_round_operations() if r["operation_type"] == "auction_settled"]
    assert {r["round_kick_id"] for r in closes} == {old, new}
    assert web3.get_transaction_receipt.await_count == 2
    assert await reconciler.discover_direct_settlements(max_log_chunks=1) == []
    assert len(events.ranges) == 2
    assert web3.get_transaction_receipt.await_count == 2


@pytest.mark.asyncio
async def test_exhausted_round_does_not_prevent_other_rounds_and_current_goes_first(session):
    repo = KickTxRepository(session)
    old = _confirmed_kick(repo, 100)
    new = _confirmed_kick(repo, 2_000_000)
    tx_hash = "0x" + "73" * 32
    reconciler, web3, events = _settlement_reconciler(session,
        [dict(blockNumber=2_000_002, transactionIndex=2, transactionHash=tx_hash)],
        {tx_hash: _receipt(block=2_000_002)})
    errors = await reconciler.discover_direct_settlements(max_log_chunks=1)
    assert [(error.error_code, error.kick_id) for error in errors] == [("known_history_limit", old)]
    assert events.ranges == [(2_000_000, 2_049_999), (100, 50_099)]
    assert [r["round_kick_id"] for r in repo.list_round_operations() if r["operation_type"] == "auction_settled"] == [new]
    assert repo.get(old)["historical_baseline"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_index,matched", [(3, True), (5, False), (7, False)])
async def test_next_completed_kick_bounds_old_round_within_same_block(session, settlement_index, matched):
    repo = KickTxRepository(session)
    old = _confirmed_kick(repo, 100)
    new = _confirmed_kick(repo, 200, transaction_index=5)
    repo.insert(_row(operation_type="resolve_auction", tx_hash="0xresolve", status="CONFIRMED",
        sell_amount="100", resolution_path=5, round_kick_id=new,
        block_number=201, transaction_index=1, mined_at=MINED_AT))
    tx_hash = "0x" + "74" * 32
    reconciler, web3, events = _settlement_reconciler(session,
        [dict(blockNumber=200, transactionIndex=settlement_index, transactionHash=tx_hash)],
        {tx_hash: _receipt(block=200, transaction_index=settlement_index)})
    assert await reconciler.discover_direct_settlements() == []
    assert events.ranges == [(100, 200)]
    closes = [r for r in repo.list_round_operations() if r["operation_type"] == "auction_settled"]
    assert [r["round_kick_id"] for r in closes] == ([old] if matched else [])
    assert web3.get_transaction_receipt.await_count == int(matched)


@pytest.mark.asyncio
async def test_older_gap_behind_completed_no_fill_is_repaired(session):
    from tidal.auction_rounds import NoFillGuard, NoFillReason
    repo = KickTxRepository(session)
    old = _confirmed_kick(repo, 100)
    new = _confirmed_kick(repo, 200)
    repo.insert(_row(operation_type="resolve_auction", tx_hash="0xresolve", status="CONFIRMED",
        sell_amount="100", resolution_path=5, round_kick_id=new,
        block_number=201, transaction_index=1, mined_at=MINED_AT))
    guard = NoFillGuard(repo, [1, 2])
    assert guard.decide(auction_address=AUCTION, token_address=TOKEN).reason_code == NoFillReason.ROUND_INCOMPLETE
    tx_hash = "0x" + "75" * 32
    reconciler, web3, _ = _settlement_reconciler(session,
        [dict(blockNumber=102, transactionIndex=2, transactionHash=tx_hash)], {tx_hash: _receipt(block=102)})
    assert await reconciler.repair_pairs({(AUCTION, TOKEN)}) == []
    assert guard.decide(auction_address=AUCTION, token_address=TOKEN).reason_code == NoFillReason.RETRY_DUE
    assert web3.get_transaction_receipt.await_args.args == (tx_hash,)


@pytest.mark.asyncio
async def test_unrelated_token_events_do_not_fetch_receipts_or_close_round(session):
    repo = KickTxRepository(session)
    _confirmed_kick(repo, 100)
    logs = [dict(blockNumber=102, transactionIndex=i, transactionHash="0x" + f"{i:064x}", args={"from": TOKEN_2}) for i in range(101)]
    reconciler, web3, events = _settlement_reconciler(session, logs, {}, head=103)
    assert await reconciler.discover_direct_settlements() == []
    web3.get_transaction_receipt.assert_not_awaited()
    assert events.filters == [{"from": Web3.to_checksum_address(TOKEN)}]
    assert len(repo.list_round_operations()) == 1


@pytest.mark.asyncio
async def test_complete_transaction_history_does_not_consume_receipt_allowance(session):
    repo = KickTxRepository(session)
    hashes = set()
    for index in range(105):
        block = 100 + index * 10
        kick_id = _confirmed_kick(repo, block)
        kick_hash = repo.get(kick_id)["tx_hash"]
        close_hash = "0x" + f"{block + 1:064x}"
        repo.insert(_row(operation_type="resolve_auction", tx_hash=close_hash, status="CONFIRMED",
            sell_amount="100", resolution_path=5, round_kick_id=kick_id,
            block_number=block + 1, transaction_index=1, mined_at=MINED_AT))
        for tx_hash in (kick_hash, close_hash):
            session.execute(models.transactions.insert().values(operation="kick", tx_hash=tx_hash,
                legacy=1, status="CONFIRMED", created_at=MINED_AT, updated_at=MINED_AT))
            hashes.add(tx_hash)
    session.commit()
    needed_id = _confirmed_kick(repo, 2_000)
    repo.update_fields(needed_id, requested_sell_amount=None)
    session.commit()
    needed_hash = repo.get(needed_id)["tx_hash"]
    reconciler = OperationReconciler(session=session, web3_client=SimpleNamespace(), auction_kicker_address=KICKER)
    reconciler.finalize_receipt = AsyncMock(return_value=None)
    assert await reconciler.reconcile_receipts(hashes | {needed_hash}) == []
    reconciler.finalize_receipt.assert_awaited_once_with(needed_hash, {})
    reconciler.finalize_receipt.reset_mock()
    assert await reconciler.reconcile_receipts(hashes) == []
    reconciler.finalize_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_business_rows_do_not_hide_pending_ledger_attempt(session):
    repo = KickTxRepository(session)
    kick = _confirmed_kick(repo, 100)
    tx_hash = repo.get(kick)["tx_hash"]
    session.execute(models.transactions.insert().values(operation="kick", tx_hash=tx_hash,
        legacy=1, status="PENDING", created_at=MINED_AT, updated_at=MINED_AT))
    session.commit()
    reconciler = OperationReconciler(session=session, web3_client=SimpleNamespace(), auction_kicker_address=KICKER)
    reconciler.finalize_receipt = AsyncMock(return_value=None)
    assert await reconciler.reconcile_receipts({tx_hash}) == []
    reconciler.finalize_receipt.assert_awaited_once_with(tx_hash, {})


@pytest.mark.asyncio
async def test_exact_settlement_repairs_beyond_chunk_limit_and_is_idempotent(session):
    repo = KickTxRepository(session)
    kick = _confirmed_kick(repo, 100)
    tx_hash = "0x" + "76" * 32
    reconciler, web3, events = _settlement_reconciler(session, [], {tx_hash: _receipt(block=5_000_000)})
    for _ in range(2):
        assert await reconciler.discover_direct_settlements(pairs={(AUCTION, TOKEN)},
            kick_id=kick, settlement_tx_hash=tx_hash, max_log_chunks=1) == []
    assert events.ranges == []
    assert len(repo.list_round_operations()) == 2
    assert repo.get(kick)["historical_baseline"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["before_kick", "after_next_kick", "wrong_token", "unfinalized", "reverted"])
async def test_exact_settlement_rejects_wrong_round_or_unproven_event(session, defect):
    repo = KickTxRepository(session)
    kick = _confirmed_kick(repo, 100)
    _confirmed_kick(repo, 200)
    tx_hash = "0x" + "77" * 32
    block = 99 if defect == "before_kick" else 201 if defect == "after_next_kick" else 150
    receipt = _receipt(block=block)
    if defect == "reverted":
        receipt["status"] = 0
    reconciler, web3, _ = _settlement_reconciler(session, [], {tx_hash: receipt}, head=120 if defect == "unfinalized" else 1_000)
    if defect == "wrong_token":
        reconciler.decode_receipt_fn = lambda *_: DecodedReceipt(settlements=(DecodedSettlement(AUCTION, TOKEN_2),))
    errors = await reconciler.discover_direct_settlements(pairs={(AUCTION, TOKEN)}, kick_id=kick, settlement_tx_hash=tx_hash)
    assert len(errors) == 1
    assert errors[0].kick_id == kick
    assert len(repo.list_round_operations()) == 2


@pytest.mark.asyncio
async def test_round_lookup_failure_does_not_discard_other_rounds(session):
    repo = KickTxRepository(session)
    old = _confirmed_kick(repo, 100)
    new = _confirmed_kick(repo, 2_000_000)
    tx_hash = "0x" + "78" * 32
    reconciler, web3, events = _settlement_reconciler(session,
        [dict(blockNumber=102, transactionIndex=2, transactionHash=tx_hash)], {tx_hash: _receipt(block=102)})
    original = events.get_logs
    async def fail_newest(**kwargs):
        if kwargs["from_block"] == 2_000_000:
            raise TimeoutError()
        return await original(**kwargs)
    events.get_logs = fail_newest
    errors = await reconciler.discover_direct_settlements(max_log_chunks=1)
    assert [(e.error_code, e.kick_id) for e in errors] == [("event_lookup_failed", new)]
    assert [r["round_kick_id"] for r in repo.list_round_operations() if r["operation_type"] == "auction_settled"] == [old]
