"""Legacy intent conversion followed by the same native finalized evidence gate."""
import json

import pytest
from sqlalchemy import select

from tidal.legacy_operations import ensure_legacy_operations
from tidal.operation_reconciler import DecodedReceipt, DecodedResolve, DecodedSweep
from tidal.persistence import models
from tests.unit.test_managed_execution import runtime, TARGET, AUCTION, TOKEN

HASH = "0x" + "12" * 32
NOW = "2026-01-01T00:00:00+00:00"


def seed(runtime, operation, preview=None, *, existing=False):
    action = {"action_id": "legacy", "action_type": "settle", "auction_address": AUCTION,
              "token_address": TOKEN, "preview_json": json.dumps(preview if preview is not None else {
                  "inspection": {"auction_address": AUCTION, "active_token": TOKEN},
                  "decision": {"operation_type": operation.replace("-", "_"), "token_address": TOKEN,
                               "balance_raw": "99999999999999999999"},
              })}
    tx = {"action_id": "legacy", "tx_index": 0, "operation": operation, "tx_hash": HASH,
          "chain_id": 1, "signer": runtime.signer.address, "nonce": 7, "to_address": TARGET,
          "data": "0x123456", "value": "0", "created_at": NOW, "updated_at": NOW, "broadcast_at": NOW}
    transaction_id = runtime.session.execute(models.transactions.insert().values(**tx, legacy=1, status="PENDING")).lastrowid
    if existing:
        runtime.session.execute(models.kick_txs.insert().values(
            id=123, run_id="api-action:legacy", operation_type=operation.replace("-", "_"),
            auction_address=AUCTION, token_address=TOKEN, tx_hash=HASH, status="SUBMITTED", created_at=NOW))
    runtime.signer.last_transaction = {"to": TARGET, "data": "0x123456", "value": 0, "chainId": 1, "nonce": 7}
    runtime.rpc.mined = True
    runtime.rpc.finalized = 102
    return transaction_id, action, tx


def convert(runtime, transaction_id, action, tx, *, transactions=None):
    ids = ensure_legacy_operations(runtime.session, action_row=action, tx_row=tx,
                                   source_transactions=transactions if transactions is not None else [tx])
    runtime.session.execute(models.kick_txs.update().where(models.kick_txs.c.id.in_(ids)).values(transaction_id=transaction_id))
    runtime.session.commit()
    return ids


@pytest.mark.parametrize("legacy,canonical", [("settle", "resolve_auction"), ("sweep-and-settle", "sweep_auction")])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("receipt_status,expected", [(1, "CONFIRMED"), (0, "REVERTED")])
@pytest.mark.asyncio
async def test_legacy_aliases_converge_without_duplicates_or_changed_creation_times(runtime, legacy, canonical, existing, receipt_status, expected):
    transaction_id, action, tx = seed(runtime, legacy, existing=existing)
    runtime.rpc.receipt_status = receipt_status
    runtime.operation_reconciler.decode_receipt_fn = lambda *_: DecodedReceipt(
        resolves=(DecodedResolve(AUCTION, TOKEN, 0, 7),), sweeps=(DecodedSweep(AUCTION, TOKEN, 7),))
    for _ in range(2):
        convert(runtime, transaction_id, action, tx)
        await runtime.executor.reconciler.reconcile(transaction_ids=[transaction_id])
        row = runtime.session.execute(select(models.kick_txs)).mappings().one()
        assert row["operation_type"] == canonical and row["status"] == expected
        assert row["created_at"] == NOW
        if existing:
            assert row["id"] == 123
        if receipt_status:
            assert row["sell_amount"] == "7"
    assert runtime.signer.calls == runtime.rpc.sends == 0


@pytest.mark.parametrize("shape", ["modern_empty", "multiple_transactions", "wrong_decision"])
@pytest.mark.asyncio
async def test_unprovable_legacy_preview_cannot_turn_into_success_from_a_receipt(runtime, shape):
    preview = {"inspection": {"auction_address": AUCTION},
               "decision": {"operation_type": "sweep_and_settle", "token_address": TOKEN}}
    if shape == "modern_empty":
        preview["preparedOperations"] = []
    if shape == "wrong_decision":
        preview["decision"]["operation_type"] = "kick"
    transaction_id, action, tx = seed(runtime, "sweep-and-settle", preview)
    source = [tx, {**tx, "tx_index": 1}] if shape == "multiple_transactions" else [tx]
    assert convert(runtime, transaction_id, action, tx, transactions=source) == set()
    await runtime.executor.reconciler.reconcile(transaction_ids=[transaction_id])
    assert runtime.executor.repository.get(transaction_id)["status"] == "REVIEW_REQUIRED"


@pytest.mark.asyncio
async def test_legacy_preview_balance_is_never_substituted_for_a_required_event(runtime):
    transaction_id, action, tx = seed(runtime, "sweep-and-settle")
    convert(runtime, transaction_id, action, tx)
    runtime.operation_reconciler.decode_receipt_fn = lambda *_: DecodedReceipt()
    await runtime.executor.reconciler.reconcile(transaction_ids=[transaction_id])
    row = runtime.session.execute(select(models.kick_txs)).mappings().one()
    assert row["status"] == "SUBMITTED" and row["sell_amount"] is None
    transaction = runtime.executor.repository.get(transaction_id)
    assert transaction["status"] == "REVIEW_REQUIRED" and "AuctionSwept" in transaction["error_message"]


@pytest.mark.asyncio
async def test_unrecorded_caller_receipt_cannot_create_or_finalize_anything(runtime):
    # The old callback used to accept partial receipt evidence directly.
    runtime.operation_reconciler.lifecycle_settings = runtime.settings
    assert await runtime.operation_reconciler.finalize_receipt(HASH, {"status": 1}) == "transaction_identity_missing"
    assert runtime.session.execute(select(models.transactions)).first() is None
    assert runtime.session.execute(select(models.kick_txs)).first() is None
