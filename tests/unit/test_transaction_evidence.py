from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.transaction_evidence import EvidenceError, observe_transaction, verify_evidence

SENDER = "0x" + "1" * 40
TARGET = "0x" + "2" * 40
HASH = "0x" + "a" * 64
BLOCK = "0x" + "b" * 64
OTHER = "0x" + "c" * 64


def evidence():
    return {
        "transaction": {
            "hash": HASH, "chainId": 1, "from": SENDER, "to": TARGET,
            "input": "0x123456", "value": 7, "nonce": 12,
            "blockNumber": 100, "blockHash": BLOCK, "transactionIndex": 2,
        },
        "receipt": {
            "transactionHash": HASH, "from": SENDER, "to": TARGET, "status": 1,
            "blockNumber": 100, "blockHash": BLOCK, "transactionIndex": 2,
        },
        "block": {"number": 100, "hash": BLOCK, "timestamp": 1789316805},
        "finalized_head": {"number": 101, "hash": OTHER},
        "chain_id": 1,
    }


def retained():
    return {
        "chain_id": 1, "signer": SENDER, "nonce": 12, "tx_hash": HASH,
        "to_address": TARGET, "data": "0x123456", "value": "7",
    }


@pytest.mark.parametrize("status", [0, 1])
def test_finalized_receipt_identity_does_not_claim_a_business_outcome(status):
    proof = evidence()
    proof["receipt"]["status"] = status
    checked = verify_evidence(retained(), **proof)
    assert checked.finalized is True
    assert checked.receipt["status"] == status


def test_included_receipt_is_provisional_until_a_later_finalized_head():
    proof = evidence()
    proof["finalized_head"] = {"number": 99, "hash": OTHER}
    assert verify_evidence(retained(), **proof).finalized is False
    proof["finalized_head"] = {"number": 100, "hash": BLOCK}
    assert verify_evidence(retained(), **proof).finalized is True


@pytest.mark.parametrize("field", list(retained()))
def test_missing_retained_fields_are_not_inferred_from_the_current_chain(field):
    saved = retained()
    saved[field] = None
    with pytest.raises(EvidenceError) as error:
        verify_evidence(saved, **evidence())
    assert error.value.code == "INCOMPLETE_IDENTITY"


@pytest.mark.parametrize("group,field,value", [
    ("transaction", "hash", OTHER),
    ("transaction", "chainId", 10),
    ("transaction", "from", TARGET),
    ("transaction", "nonce", 13),
    ("transaction", "to", SENDER),
    ("transaction", "input", "0x123457"),
    ("transaction", "value", 8),
    ("receipt", "transactionHash", OTHER),
    ("receipt", "from", TARGET),
    ("receipt", "to", SENDER),
    ("receipt", "status", 2),
])
def test_identity_and_intent_mismatches_are_rejected(group, field, value):
    proof = evidence()
    proof[group][field] = value
    with pytest.raises(EvidenceError) as error:
        verify_evidence(retained(), **proof)
    assert error.value.code == "INTENT_MISMATCH"


@pytest.mark.parametrize("group,field,value", [
    ("block", "hash", OTHER),
    ("block", "number", 101),
    ("receipt", "blockHash", OTHER),
    ("transaction", "blockHash", OTHER),
    ("transaction", "blockNumber", 101),
    ("transaction", "transactionIndex", 1),
])
def test_receipt_canonicality_checks_reject_reorg_or_conflicting_block_evidence(group, field, value):
    proof = evidence()
    proof[group][field] = value
    with pytest.raises(EvidenceError) as error:
        verify_evidence(retained(), **proof)
    assert error.value.code == "NONCANONICAL_RECEIPT"


def test_finalized_head_at_receipt_height_must_have_the_same_hash():
    proof = evidence()
    proof["finalized_head"] = {"number": 100, "hash": OTHER}
    with pytest.raises(EvidenceError) as error:
        verify_evidence(retained(), **proof)
    assert error.value.code == "NONCANONICAL_RECEIPT"


@pytest.mark.parametrize("group,field", [
    ("transaction", "chainId"), ("receipt", "blockHash"), ("block", "hash"),
    ("finalized_head", "number"), ("finalized_head", "hash"),
])
def test_incomplete_rpc_response_is_not_accepted(group, field):
    proof = evidence()
    del proof[group][field]
    with pytest.raises(EvidenceError) as error:
        verify_evidence(retained(), **proof)
    assert error.value.code == "INCOMPLETE_RPC_EVIDENCE"


@pytest.mark.asyncio
async def test_observation_reads_finalized_head_before_fresh_receipt_and_block():
    calls = []
    proof = evidence()

    async def get_block(identifier):
        calls.append(("block", identifier))
        return proof["finalized_head"] if identifier == "finalized" else proof["block"]

    async def get_receipt(tx_hash, *, timeout_seconds):
        calls.append(("receipt", tx_hash))
        assert calls[0] == ("block", "finalized")
        assert timeout_seconds == 2
        return proof["receipt"]

    rpc = SimpleNamespace(
        get_block=get_block, get_chain_id=AsyncMock(return_value=1),
        get_transaction_receipt=get_receipt,
        get_transaction=AsyncMock(return_value=proof["transaction"]),
    )
    checked = await observe_transaction(rpc, retained())
    assert checked.finalized
    assert calls == [("block", "finalized"), ("receipt", HASH), ("block", 100)]


@pytest.mark.asyncio
async def test_rpc_failure_preserves_the_retained_attempt():
    saved = retained()
    original = deepcopy(saved)
    rpc = SimpleNamespace(get_block=AsyncMock(side_effect=TimeoutError("unavailable")))
    with pytest.raises(TimeoutError):
        await observe_transaction(rpc, saved)
    assert saved == original


def test_missing_legacy_identity_comes_from_exact_transaction_without_account_nonce_reads():
    from tidal.transaction_evidence import hydrate_legacy_identity

    minimal = {"tx_hash": HASH, "data": retained()["data"]}
    actual = hydrate_legacy_identity(minimal, evidence()["transaction"], chain_id=1)
    assert actual == retained()


@pytest.mark.parametrize("field,value", [("nonce", 999), ("signer", "0x" + "9" * 40), ("data", "0xdeadbeef")])
def test_legacy_hydration_refuses_to_rewrite_retained_intent(field, value):
    from tidal.transaction_evidence import hydrate_legacy_identity

    with pytest.raises(EvidenceError) as error:
        hydrate_legacy_identity({**retained(), field: value}, evidence()["transaction"], chain_id=1)
    assert error.value.code == "LEGACY_CONFLICT"
