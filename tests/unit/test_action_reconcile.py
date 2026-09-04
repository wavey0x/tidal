import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from hexbytes import HexBytes
from sqlalchemy import select

from tidal.api.app import create_app
from tidal.api.dependencies import get_operator
from tidal.api.services.action_audit import create_prepared_action, get_action, record_broadcast
from tidal.api.services.action_reconcile import reconcile_pending_actions
from tidal.operation_reconciler import DecodedKick, DecodedReceipt, OperationReconciler, _matches_prepared_transaction
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.persistence.repositories import APIActionRepository
from tidal.config import Settings

SENDER = "0x" + "1" * 40
KICKER = "0x" + "2" * 40
AUCTION = "0x" + "3" * 40
TOKEN = "0x" + "4" * 40
TX_HASH = "0x" + "5" * 64
NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'actions.db'}")
    models.metadata.create_all(db.engine)
    yield db
    db.engine.dispose()


def seed(session, *, operation="kick", sender=SENDER):
    action_id = create_prepared_action(
        session, operator_id="operator", action_type=operation, sender=sender,
        request_payload={},
        preview_payload={"preparedOperations": [{
            "operation": operation, "sourceType": "strategy", "sourceAddress": SENDER,
            "auctionAddress": AUCTION, "tokenAddress": TOKEN, "sellAmount": "100", "txIndex": 0,
        }]},
        transactions=[{"operation": operation, "to": KICKER, "data": "0x1234", "value": "0x0", "chainId": 1}],
    )
    record_broadcast(session, action_id, tx_index=0, tx_hash=TX_HASH, broadcast_at=NOW)
    return action_id


def rpc(*, status=1):
    receipt = {"transactionHash": TX_HASH, "to": KICKER, "status": status, "blockNumber": 12,
               "transactionIndex": 0, "gasUsed": 21000, "effectiveGasPrice": 1000000000, "logs": []}
    transaction = {"hash": TX_HASH, "to": KICKER, "input": "0x1234", "value": 0, "from": SENDER, "chainId": 1}
    return SimpleNamespace(
        get_transaction=AsyncMock(return_value=transaction), get_chain_id=AsyncMock(return_value=1),
        get_transaction_receipt=AsyncMock(return_value=receipt),
        get_block=AsyncMock(return_value={"timestamp": 1767225600}), close=AsyncMock(),
    ), receipt, transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [(1, "CONFIRMED"), (0, "REVERTED")])
async def test_verified_receipt_converges_both_ledgers_and_replay_cannot_downgrade(database, status, expected):
    with database.session() as session:
        action_id = seed(session)
        web3, receipt, _ = rpc(status=status)
        reconciler = OperationReconciler(
            session=session, web3_client=web3, auction_kicker_address=KICKER,
            decode_receipt_fn=lambda *_: DecodedReceipt(kicks=(DecodedKick(SENDER, AUCTION, TOKEN, 100, 100),)),
        )
        # A pre-upgrade client report is not a barrier to verified correction.
        session.execute(models.api_action_transactions.update().values(receipt_status="FAILED", error_message="old report"))
        session.commit()
        await reconciler.finalize_receipt(TX_HASH, receipt)
        record_broadcast(session, action_id, tx_index=0, tx_hash=TX_HASH, broadcast_at=NOW)
        detail = get_action(session, action_id)
        assert detail["status"] == expected
        assert detail["transactions"][0]["verifiedAt"]
        assert detail["transactions"][0]["errorMessage"] is None
        operation = session.execute(select(models.kick_txs)).mappings().one()
        assert operation["status"] == expected
        assert operation["gas_used"] == 21000


@pytest.mark.asyncio
async def test_pending_deploy_recovers_after_lookup_failure_without_kick_rows(database):
    with database.session() as session:
        action_id = seed(session, operation="deploy", sender=None)
    web3, receipt, _ = rpc()
    web3.get_transaction_receipt.side_effect = [TimeoutError(), receipt]
    settings = SimpleNamespace(tidal_api_receipt_reconcile_threshold_seconds=0, auction_kicker_address=KICKER, chain_id=1)
    await reconcile_pending_actions(database, settings, web3)
    with database.session() as session:
        assert get_action(session, action_id)["status"] == "BROADCAST_REPORTED"
    await reconcile_pending_actions(database, settings, web3)
    with database.session() as session:
        assert get_action(session, action_id)["status"] == "CONFIRMED"
        assert session.execute(select(models.kick_txs)).first() is None


@pytest.mark.parametrize("field,value", [
    ("to", AUCTION), ("input", "0x1235"), ("value", 1), ("from", AUCTION),
    ("chainId", 2), ("hash", "0x" + "6" * 64),
])
def test_transaction_binding_checks_each_prepared_field(field, value):
    _, receipt, transaction = rpc()
    prepared = {"chain_id": 1, "to_address": KICKER, "data": "0x1234", "value": "0x0", "sender": SENDER}
    assert _matches_prepared_transaction(prepared, transaction, receipt, TX_HASH, 1)
    assert not _matches_prepared_transaction(prepared, {**transaction, field: value}, receipt, TX_HASH, 1)
    assert not _matches_prepared_transaction(prepared, transaction, receipt, TX_HASH, 2)


@pytest.mark.asyncio
async def test_mismatched_action_is_not_finalized(database):
    with database.session() as session:
        action_id = seed(session)
        web3, receipt, transaction = rpc()
        transaction["input"] = "0x5678"
        reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
        assert await reconciler.finalize_receipt(TX_HASH, receipt) == "transaction_intent_mismatch"
        assert get_action(session, action_id)["status"] == "BROADCAST_REPORTED"
        assert session.execute(select(models.kick_txs.c.status)).scalar_one() == "SUBMITTED"


def test_pending_query_is_bounded_and_includes_old_terminal_reports(database):
    with database.session() as session:
        seed(session, operation="deploy")
        session.execute(models.api_action_transactions.update().values(receipt_status="FAILED", updated_at=NOW))
        session.commit()
        repo = APIActionRepository(session)
        assert len(repo.pending_receipt_transactions(older_than="2027", limit=1)) == 1
        assert repo.pending_receipt_transactions(older_than="2027", limit=0) == []


@pytest.mark.parametrize("status,expected", [(1, "CONFIRMED"), (0, "REVERTED")])
def test_receipt_route_uses_chain_evidence_and_recovers_after_timeout(database, monkeypatch, status, expected):
    settings = Settings(db_path=database.engine.url.database, rpc_url="http://rpc.invalid")
    app = create_app(settings)
    app.dependency_overrides[get_operator] = lambda: SimpleNamespace(label="operator")
    with database.session() as session:
        action_id = seed(session, operation="deploy")
    web3, receipt, _ = rpc(status=status)
    web3.get_transaction_receipt.side_effect = [TimeoutError(), receipt, receipt]
    monkeypatch.setattr("tidal.api.routes.actions.build_web3_client", lambda _: web3)
    client = TestClient(app)
    url = f"/api/v1/tidal/actions/{action_id}/receipt"
    hint = {"txIndex": 0, "receiptStatus": "FAILED", "blockNumber": 999, "errorMessage": "client report"}
    pending = client.post(url, json=hint).json()
    assert pending["data"]["status"] == "BROADCAST_REPORTED"
    assert pending["warnings"]
    for _ in range(2):
        verified = client.post(url, json=hint).json()
        assert verified["data"]["status"] == expected
        assert verified["data"]["transactions"][0]["blockNumber"] == 12
        assert verified["data"]["transactions"][0]["errorMessage"] is None
    assert web3.close.await_count == 3
    app.state.database.engine.dispose()


@pytest.mark.asyncio
async def test_finalization_failure_rolls_back_both_ledgers(database, monkeypatch):
    with database.session() as session:
        action_id = seed(session)
        web3, receipt, _ = rpc()
        reconciler = OperationReconciler(
            session=session, web3_client=web3, auction_kicker_address=KICKER,
            decode_receipt_fn=lambda *_: DecodedReceipt(kicks=(DecodedKick(SENDER, AUCTION, TOKEN, 100, 100),)),
        )

        def fail(*args, **kwargs):
            raise RuntimeError("incomplete finalization")

        monkeypatch.setattr("tidal.operation_reconciler.record_verified_receipt", fail)
        with pytest.raises(RuntimeError, match="incomplete finalization"):
            await reconciler.finalize_receipt(TX_HASH, receipt)
    with database.session() as session:
        assert get_action(session, action_id)["status"] == "BROADCAST_REPORTED"
        assert session.execute(select(models.kick_txs.c.status)).scalar_one() == "SUBMITTED"


@pytest.mark.asyncio
async def test_api_lifespan_runs_reconciler_and_closes_client_on_shutdown(database, monkeypatch):
    settings = Settings(db_path=database.engine.url.database, rpc_url="http://rpc.invalid")
    web3, _, _ = rpc()
    started = asyncio.Event()

    async def check(*args):
        started.set()

    monkeypatch.setattr("tidal.api.services.action_reconcile.build_web3_client", lambda _: web3)
    monkeypatch.setattr("tidal.api.services.action_reconcile.reconcile_pending_actions", check)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        web3.close.assert_not_awaited()
    web3.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalization_repairs_missing_operations_before_verifying_action(database):
    with database.session() as session:
        action_id = seed(session)
        # Model an interrupted pre-upgrade broadcast after its hash was committed.
        session.execute(models.kick_txs.delete())
        session.commit()
    web3, _, _ = rpc()
    settings = SimpleNamespace(tidal_api_receipt_reconcile_threshold_seconds=0, auction_kicker_address=KICKER, chain_id=1)
    # A reverted receipt needs no event decoding but must materialize its operation.
    web3.get_transaction_receipt.return_value["status"] = 0
    await reconcile_pending_actions(database, settings, web3)
    with database.session() as session:
        assert get_action(session, action_id)["status"] == "REVERTED"
        assert session.execute(select(models.kick_txs.c.status)).scalar_one() == "REVERTED"


def test_broadcast_materialization_failure_leaves_neither_hash_nor_operations(database, monkeypatch):
    with database.session() as session:
        action_id = create_prepared_action(
            session, operator_id="operator", action_type="deploy", sender=SENDER,
            request_payload={}, preview_payload={},
            transactions=[{"operation": "deploy", "to": KICKER, "data": "0x1234", "chainId": 1}],
        )

        def fail(*args, **kwargs):
            raise RuntimeError("operation write failed")

        monkeypatch.setattr("tidal.api.services.action_audit.ensure_action_operations", fail)
        with pytest.raises(RuntimeError, match="operation write failed"):
            record_broadcast(session, action_id, tx_index=0, tx_hash=TX_HASH, broadcast_at=NOW)
    with database.session() as session:
        assert get_action(session, action_id)["transactions"][0]["txHash"] is None
        assert session.execute(select(models.kick_txs)).first() is None


@pytest.mark.asyncio
async def test_only_operations_for_verified_tx_index_are_finalized(database):
    second_auction = "0x" + "6" * 40
    with database.session() as session:
        action_id = seed(session)
        first = APIActionRepository(session).get_action_transactions(action_id)[0]
        session.execute(models.api_action_transactions.insert().values(
            **{key: value for key, value in first.items() if key != "id"} | {"tx_index": 1, "data": "0x5678"},
        ))
        action = APIActionRepository(session).get_action(action_id)
        preview = json.loads(action["preview_json"])
        preview["preparedOperations"].append({
            **preview["preparedOperations"][0], "txIndex": 1, "auctionAddress": second_auction,
        })
        session.execute(models.api_actions.update().values(preview_json=json.dumps(preview)))
        session.commit()
        record_broadcast(session, action_id, tx_index=1, tx_hash=TX_HASH, broadcast_at=NOW)
        web3, receipt, _ = rpc(status=0)
        reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
        assert await reconciler.finalize_receipt(TX_HASH, receipt) == "transaction_intent_mismatch"
        txs = get_action(session, action_id)["transactions"]
        assert [tx["receiptStatus"] for tx in txs] == ["REVERTED", None]
        operations = session.execute(select(models.kick_txs).order_by(models.kick_txs.c.id)).mappings().all()
        assert [(row["auction_address"], row["status"]) for row in operations] == [
            (AUCTION, "REVERTED"), (second_auction, "SUBMITTED"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["get_transaction", "get_chain_id"])
async def test_added_lookup_failures_do_not_stop_scanner_reconciliation(database, lookup):
    with database.session() as session:
        action_id = seed(session, operation="deploy")
        web3, receipt, _ = rpc()
        native_hash = "0x" + "7" * 64
        session.execute(models.kick_txs.insert().values(
            run_id="native", operation_type="kick", auction_address=AUCTION,
            token_address=TOKEN, status="SUBMITTED", tx_hash=native_hash, created_at=NOW,
        ))
        session.commit()

        async def lookup_receipt(tx_hash, **kwargs):
            return receipt if tx_hash == TX_HASH else {**receipt, "transactionHash": native_hash, "status": 0}

        web3.get_transaction_receipt.side_effect = lookup_receipt
        getattr(web3, lookup).side_effect = TimeoutError()
        reconciler = OperationReconciler(session=session, web3_client=web3, auction_kicker_address=KICKER)
        errors = await reconciler.reconcile_receipts([TX_HASH, native_hash])
        assert len(errors) == 1
        assert errors[0].error_code == "receipt_lookup_failed"
        assert session.execute(select(models.kick_txs.c.status)).scalar_one() == "REVERTED"
        assert get_action(session, action_id)["status"] == "BROADCAST_REPORTED"
        getattr(web3, lookup).side_effect = None
        assert await reconciler.reconcile_receipts([TX_HASH]) == []
        assert get_action(session, action_id)["status"] == "CONFIRMED"


def test_matching_accepts_web3_bytes_and_unspecified_sender():
    _, receipt, transaction = rpc()
    transaction.pop("chainId")
    transaction.update(hash=HexBytes(TX_HASH), input=HexBytes("0x1234"), to=KICKER.upper())
    receipt["transactionHash"] = HexBytes(TX_HASH)
    prepared = {"chain_id": 1, "to_address": KICKER, "data": "0x1234", "value": "0x00", "sender": None}
    assert _matches_prepared_transaction(prepared, transaction, receipt, TX_HASH, 1)
