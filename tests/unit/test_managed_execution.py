import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from eth_utils import keccak
from sqlalchemy import select
from web3 import Web3
from sqlalchemy.exc import IntegrityError
from web3.exceptions import TransactionNotFound

from tidal.config import Settings
from tidal.execution import ManagedExecutor
from tidal.lifecycle import LifecycleError, activation_binding, inspect_database, write_activation
from tidal.migrations import run_migrations
from tidal.operation_reconciler import DecodedKick, DecodedReceipt, DecodedSettlement, OperationReconciler
from tidal.persistence import models
from tidal.persistence.db import Database

TARGET = "0x" + "2" * 40
AUCTION = "0x" + "3" * 40
TOKEN = "0x" + "4" * 40
TOKEN_2 = "0x" + "5" * 40
SOURCE = "0x" + "6" * 40
BLOCK = "0x" + "a" * 64
OTHER_BLOCK = "0x" + "b" * 64


class FixtureSigner:
    def __init__(self):
        self.account = Account.from_key(bytes.fromhex("01" * 32))
        self.address = self.account.address.lower()
        self.checksum_address = self.account.address
        self.calls = 0
        self.last_transaction = None

    def sign_transaction(self, tx):
        self.calls += 1
        self.last_transaction = dict(tx)
        return bytes(self.account.sign_transaction(tx).raw_transaction)


class FixtureRPC:
    def __init__(self, signer, path):
        self.signer = signer
        self.path = path
        self.sends = 0
        self.hash = None
        self.mined = False
        self.finalized = 100
        self.timestamp = int(time.time())
        self.receipt_status = 1
        self.lost_response = False
        self.stop_after_commit = False
        self.receipt_block_hash = BLOCK
        self.pending_nonce = 7

    async def get_chain_id(self):
        return 1

    def contract(self, address, abi):
        return Web3().eth.contract(address=Web3.to_checksum_address(address), abi=abi)

    async def get_block(self, identifier):
        number = self.finalized if identifier == "finalized" else 103 if identifier == "latest" else identifier
        return {"number": number, "hash": BLOCK if number == 101 else OTHER_BLOCK, "timestamp": self.timestamp}

    async def get_transaction_count(self, address, block_identifier="pending"):
        return self.pending_nonce if block_identifier == "pending" else 7

    async def send_raw_transaction(self, signed):
        if self.stop_after_commit:
            raise KeyboardInterrupt("simulated process loss before RPC acceptance")
        self.sends += 1
        self.hash = "0x" + keccak(signed).hex()
        # A separate connection must see committed identity and every linked
        # operation before any outgoing submission can reach the node.
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT id, tx_hash, nonce, signer FROM transactions WHERE legacy=0").fetchone()
            assert row[1:] == (self.hash, 7, self.signer.address)
            assert connection.execute("SELECT count(*) FROM kick_txs WHERE transaction_id=?", (row[0],)).fetchone()[0] >= 1
        if self.lost_response:
            raise TimeoutError("simulated response loss")
        return self.hash

    async def get_transaction_receipt(self, tx_hash, *, timeout_seconds):
        assert timeout_seconds == 2
        if not self.mined:
            raise TransactionNotFound(tx_hash)
        return {
            "transactionHash": tx_hash, "from": self.signer.address, "to": TARGET,
            "status": self.receipt_status, "blockNumber": 101, "blockHash": self.receipt_block_hash,
            "transactionIndex": 2, "gasUsed": 21000, "effectiveGasPrice": 1000000000, "logs": [],
        }

    async def get_transaction(self, tx_hash):
        return {
            **self.signer.last_transaction, "hash": tx_hash, "from": self.signer.address,
            "input": self.signer.last_transaction["data"], "blockNumber": 101,
            "blockHash": BLOCK, "transactionIndex": 2,
        }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    path = tmp_path / "tidal.db"
    run_migrations(f"sqlite:///{path}")
    signer = FixtureSigner()
    settings = Settings(DB_PATH=path, AUCTION_KICKER_ADDRESS=TARGET, MANAGED_SIGNERS={"scan": signer.address, "kick": signer.address})
    database = Database(settings.database_url)
    session = database.session()
    binding = activation_binding(inspect_database(path)["database_identity"], 1, settings.managed_signers)
    write_activation(tmp_path / "activation.json", binding)
    rpc = FixtureRPC(signer, path)
    reconciler = OperationReconciler(
        session=session, web3_client=rpc, auction_kicker_address=TARGET,
        decode_receipt_fn=lambda *_: DecodedReceipt(kicks=tuple(
            DecodedKick(SOURCE, AUCTION, token, 100, 100) for token in (TOKEN, TOKEN_2)
        )),
    )
    executor = ManagedExecutor(
        settings=settings, session=session, web3_client=rpc, signer=signer,
        profile="kick", operation_reconciler=reconciler,
    )
    yield SimpleNamespace(
        path=path, settings=settings, session=session, signer=signer, rpc=rpc,
        executor=executor, operation_reconciler=reconciler,
    )
    session.close()
    database.engine.dispose()


def operation(token=TOKEN):
    return {
        "run_id": "fixture", "operation_type": "kick", "source_type": "strategy",
        "source_address": SOURCE, "auction_address": AUCTION, "token_address": token,
        "requested_sell_amount": "100", "created_at": "2026-09-13T00:00:00+00:00",
    }


async def submit(runtime, operations=None):
    return await runtime.executor.submit(
        transaction={"to": TARGET, "data": "0x123456", "value": 0, "chainId": 1,
                     "gas": 100000, "type": 2, "maxFeePerGas": 2000000000, "maxPriorityFeePerGas": 1000000000},
        operations=operations if operations is not None else [operation()], action="kick",
    )


@pytest.mark.asyncio
async def test_identity_and_all_batch_operations_commit_before_broadcast(runtime):
    result = await submit(runtime, [operation(), operation(TOKEN_2)])
    assert result["status"] == "PENDING"
    assert len(result["operation_ids"]) == 2
    assert runtime.rpc.sends == 1
    assert runtime.session.execute(select(models.transactions)).mappings().one()["legacy"] == 0
    rows = runtime.session.execute(select(models.kick_txs)).mappings().all()
    assert {row["transaction_id"] for row in rows} == {result["id"]}
    assert {row["created_at"] for row in rows} == {"2026-09-13T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_persistence_failure_never_broadcasts_or_leaves_a_partial_ledger(runtime):
    bad = operation()
    del bad["token_address"]
    with pytest.raises(IntegrityError):
        await submit(runtime, [operation(), bad])
    assert runtime.rpc.sends == 0
    assert runtime.session.execute(select(models.transactions)).first() is None
    assert runtime.session.execute(select(models.kick_txs)).first() is None


@pytest.mark.asyncio
async def test_expired_preparation_cannot_sign_after_operator_or_rpc_delay(runtime):
    with pytest.raises(LifecycleError) as error:
        await runtime.executor.submit(
            transaction={"to": TARGET, "data": "0x123456", "value": 0, "chainId": 1,
                         "gas": 100000, "type": 2, "maxFeePerGas": 2000000000, "maxPriorityFeePerGas": 1000000000},
            operations=[operation()], action="kick",
            prepared_at_monotonic=time.monotonic() - 301,
        )
    assert error.value.code == "STALE_PREPARATION"
    assert runtime.signer.calls == runtime.rpc.sends == 0
    assert runtime.session.execute(select(models.transactions)).first() is None


@pytest.mark.asyncio
async def test_lost_response_is_retained_and_cannot_become_a_second_attempt(runtime):
    runtime.rpc.lost_response = True
    first = await submit(runtime)
    assert first["status"] == "RECORDED"
    assert first["tx_hash"] == runtime.rpc.hash
    with pytest.raises(LifecycleError) as error:
        await submit(runtime)
    assert error.value.code == "UNRESOLVED_ATTEMPTS"
    assert runtime.rpc.sends == runtime.signer.calls == 1


@pytest.mark.asyncio
async def test_process_loss_after_commit_retains_identity_without_rebroadcast(runtime):
    runtime.rpc.stop_after_commit = True
    with pytest.raises(KeyboardInterrupt):
        await submit(runtime)
    row = runtime.session.execute(select(models.transactions)).mappings().one()
    assert row["status"] == "RECORDED"
    runtime.rpc.stop_after_commit = False
    with pytest.raises(LifecycleError):
        await submit(runtime)
    assert runtime.rpc.sends == 0
    assert runtime.signer.calls == 1


@pytest.mark.asyncio
async def test_inclusion_does_not_apply_business_outcomes_until_finality(runtime):
    runtime.rpc.mined = True
    result = await submit(runtime)
    assert result["status"] == "INCLUDED"
    row = runtime.session.execute(select(models.kick_txs)).mappings().one()
    assert row["status"] == "SUBMITTED"
    assert row["sell_amount"] is None
    runtime.rpc.finalized = 102
    assert await runtime.executor.reconciler.reconcile() == []
    assert runtime.executor.repository.get(result["id"])["status"] == "CONFIRMED"
    row = runtime.session.execute(select(models.kick_txs)).mappings().one()
    assert row["status"] == "CONFIRMED"
    assert row["sell_amount"] == "100"
    assert row["created_at"] == "2026-09-13T00:00:00+00:00"


@pytest.mark.asyncio
async def test_evm_success_without_required_business_events_stays_in_review(runtime):
    runtime.rpc.mined = True
    runtime.rpc.finalized = 102
    runtime.operation_reconciler.decode_receipt_fn = lambda *_: DecodedReceipt()
    result = await submit(runtime)
    assert result["status"] == "REVIEW_REQUIRED"
    row = runtime.session.execute(select(models.kick_txs)).mappings().one()
    assert row["status"] == "SUBMITTED"
    assert row["sell_amount"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("event_present", [True, False])
async def test_native_token_enablement_links_identity_and_requires_finalized_event(runtime, event_present):
    from tidal.persistence.repositories import AuctionEnabledTokenRepository, KickTxRepository
    from tidal.scanner.auction_token_enabler import (
        AuctionEnableCandidate, AuctionEnableSource, AuctionTokenEnablementService, AuctionTokenEnablementStats,
    )

    runtime.rpc.mined = True
    service = AuctionTokenEnablementService(
        web3_client=runtime.rpc, auction_state_reader=None, signer=runtime.signer,
        kick_tx_repository=KickTxRepository(runtime.session),
        auction_enabled_token_repository=AuctionEnabledTokenRepository(runtime.session),
        base_fee_cap_gwei=1, max_priority_fee_gwei=1, max_gas_limit=500000,
        chain_id=1, settings=runtime.settings, managed_executor=runtime.executor,
    )
    candidate = AuctionEnableCandidate(
        source=AuctionEnableSource("strategy", SOURCE, AUCTION, TOKEN_2, True),
        token_address=TOKEN, decimals=18, balance_raw=100, normalized_balance="100", token_symbol="TEST",
    )
    runtime.operation_reconciler.decode_receipt_fn = lambda *_: DecodedReceipt(
        enabled=(DecodedSettlement(AUCTION, TOKEN),) if event_present else (),
    )
    stats = AuctionTokenEnablementStats()
    assert await service._send_batch(
        run_id="fixture", batch=[candidate], gas_estimate=100000,
        base_fee_gwei=0.1, priority_fee_wei=1000000000, stats=stats,
    ) == []
    assert stats.tokens_confirmed == 0
    assert stats.enable_transactions_submitted == 1
    assert runtime.session.execute(select(models.auction_enabled_tokens_latest)).first() is None
    runtime.rpc.finalized = 102
    await runtime.executor.reconciler.reconcile()
    tx = runtime.session.execute(select(models.transactions)).mappings().one()
    op = runtime.session.execute(select(models.kick_txs)).mappings().one()
    assert op["transaction_id"] == tx["id"]
    assert tx["status"] == ("CONFIRMED" if event_present else "REVIEW_REQUIRED")
    enabled = runtime.session.execute(select(models.auction_enabled_tokens_latest)).mappings().all()
    assert len(enabled) == int(event_present)
    assert runtime.rpc.sends == runtime.signer.calls == 1


@pytest.mark.asyncio
async def test_reverted_finalized_transaction_is_not_a_success(runtime):
    runtime.rpc.mined = True
    runtime.rpc.finalized = 102
    runtime.rpc.receipt_status = 0
    result = await submit(runtime)
    assert result["status"] == "REVERTED"
    assert runtime.session.execute(select(models.kick_txs.c.status)).scalar_one() == "REVERTED"


@pytest.mark.asyncio
async def test_conflicting_receipt_block_remains_held(runtime):
    runtime.rpc.mined = True
    runtime.rpc.finalized = 102
    runtime.rpc.receipt_block_hash = OTHER_BLOCK
    result = await submit(runtime)
    assert result["status"] == "REVIEW_REQUIRED"
    assert runtime.session.execute(select(models.kick_txs.c.status)).scalar_one() == "SUBMITTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("finalized", [100, 102])
async def test_explicit_replacement_requires_finality_and_never_confirms_original_business(runtime, finalized):
    original = await submit(runtime)
    replacement_hash = "0x" + "ef" * 32
    original_get_tx = runtime.rpc.get_transaction

    async def replacement_tx(tx_hash):
        tx = await original_get_tx(tx_hash)
        return {**tx, "input": "0x", "value": 0}

    async def receipt(tx_hash, *, timeout_seconds):
        if tx_hash == original["tx_hash"]:
            raise TransactionNotFound(tx_hash)
        return {"transactionHash": replacement_hash, "from": runtime.signer.address, "to": TARGET,
                "status": 1, "blockNumber": 101, "blockHash": BLOCK, "transactionIndex": 2}

    runtime.rpc.get_transaction = replacement_tx
    runtime.rpc.get_transaction_receipt = receipt
    runtime.rpc.finalized = finalized
    if finalized == 100:
        with pytest.raises(LifecycleError, match="not finalized"):
            await runtime.executor.reconciler.resolve_with_replacement(
                transaction_id=original["id"], replacement_hash=replacement_hash, note="Fixture cancellation reviewed",
            )
        assert runtime.executor.repository.get(original["id"])["status"] == "PENDING"
    else:
        result = await runtime.executor.reconciler.resolve_with_replacement(
            transaction_id=original["id"], replacement_hash=replacement_hash, note="Fixture cancellation reviewed",
        )
        assert result["status"] == "SUPERSEDED"
        assert result["resolved_by_hash"] == replacement_hash
        row = runtime.session.execute(select(models.kick_txs)).mappings().one()
        assert row["status"] == "SUPERSEDED"
        assert row["sell_amount"] is None
    assert runtime.rpc.sends == runtime.signer.calls == 1


@pytest.mark.asyncio
async def test_higher_nonce_is_not_evidence_of_the_original_business_outcome(runtime):
    original = await submit(runtime)
    wrong = {**await runtime.rpc.get_transaction("0x" + "ef" * 32), "nonce": 8}
    runtime.rpc.get_transaction = AsyncMock(return_value=wrong)
    from tidal.transaction_evidence import EvidenceError
    with pytest.raises(EvidenceError) as error:
        await runtime.executor.reconciler.resolve_with_replacement(
            transaction_id=original["id"], replacement_hash=wrong["hash"], note="Fixture review",
        )
    assert error.value.code == "LEGACY_CONFLICT"
    assert runtime.executor.repository.get(original["id"])["status"] == "PENDING"
    assert runtime.rpc.sends == 1


@pytest.mark.asyncio
async def test_two_profiles_with_the_same_signer_share_the_unresolved_guard(runtime):
    await submit(runtime)
    runtime.executor.profile = "scan"
    with pytest.raises(LifecycleError) as error:
        await submit(runtime)
    assert error.value.code == "UNRESOLVED_ATTEMPTS"
    assert runtime.rpc.sends == 1


@pytest.mark.asyncio
async def test_missing_activation_and_stale_chain_prevent_signing(runtime):
    runtime.rpc.timestamp -= 4000
    with pytest.raises(LifecycleError) as error:
        await submit(runtime)
    assert error.value.code == "STALE_CHAIN"
    (runtime.settings.resolved_home_path / "activation.json").unlink()
    with pytest.raises(LifecycleError) as error:
        await submit(runtime)
    assert error.value.code == "HELD"
    assert runtime.signer.calls == runtime.rpc.sends == 0


@pytest.mark.asyncio
async def test_unexpected_pending_nonce_prevents_signing(runtime):
    runtime.rpc.pending_nonce = 8
    with pytest.raises(LifecycleError) as error:
        await submit(runtime)
    assert error.value.code == "UNEXPECTED_NONCE"
    assert runtime.signer.calls == 0


@pytest.mark.asyncio
async def test_transport_layer_does_not_retry_raw_submission(monkeypatch):
    import aiohttp
    from unittest.mock import AsyncMock
    from tidal.chain.web3_client import Web3Client

    client = Web3Client("http://fixture.invalid", timeout_seconds=1, retry_attempts=5)
    transport = AsyncMock(side_effect=aiohttp.ClientError("lost response"))
    monkeypatch.setattr(client.w3.provider._request_session_manager, "async_make_post_request", transport)
    try:
        with pytest.raises(aiohttp.ClientError):
            await client.send_raw_transaction(b"fixture payload")
        assert transport.await_count == 1
        assert client.w3.provider.exception_retry_configuration is None
    finally:
        await client.close()
