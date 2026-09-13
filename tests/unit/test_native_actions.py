"""Real sender/ledger tests for native manually prepared auction actions."""
import copy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from tidal.persistence import models
import tidal.native_actions as actions
from tests.unit.test_managed_execution import runtime, TARGET, AUCTION, TOKEN, TOKEN_2, SOURCE


@pytest.fixture
def prepared(runtime, monkeypatch):
    payload = {
        "preview": {"preparedOperations": [
            {"operation": "enable-tokens", "txIndex": 0, "auctionAddress": AUCTION,
             "tokenAddress": token, "sourceType": "strategy", "sourceAddress": SOURCE}
            for token in (TOKEN, TOKEN_2)
        ]},
        "transactions": [{"operation": "enable-tokens", "to": TARGET, "data": "0x123456",
                          "chainId": 1, "sender": runtime.signer.address, "value": "0x0",
                          "gasEstimate": 100000, "gasLimit": 120000}],
    }
    runtime.rpc.get_base_fee = AsyncMock(return_value=1000000000)
    monkeypatch.setattr(actions, "resolve_priority_fee_wei", AsyncMock(return_value=1000000000))
    monkeypatch.setattr(actions, "build_web3_client", lambda _: runtime.rpc)
    monkeypatch.setattr(actions, "close_client", AsyncMock())
    async def prepare(**kwargs):
        return "ok", [], copy.deepcopy(payload)
    monkeypatch.setattr(actions, "prepare_enable_tokens_action", prepare)
    return payload


async def run(runtime, **kwargs):
    return await actions.run_auction_action(settings=runtime.settings, session=runtime.session,
        signer=runtime.signer, action="enable_tokens", auction=AUCTION, **kwargs)


@pytest.mark.asyncio
async def test_all_token_links_are_committed_before_the_single_native_send(runtime, prepared):
    result = await run(runtime)
    assert result["transactions"][0]["status"] == "PENDING"
    assert runtime.rpc.sends == runtime.signer.calls == 1
    rows = runtime.session.execute(select(models.kick_txs)).mappings().all()
    assert {row["token_address"] for row in rows} == {TOKEN, TOKEN_2}
    assert {row["operation_type"] for row in rows} == {"enable_tokens"}
    assert len({row["transaction_id"] for row in rows}) == 1


@pytest.mark.asyncio
async def test_pending_first_batch_leaves_remaining_work_unsubmitted(runtime, prepared):
    prepared["transactions"].append(copy.deepcopy(prepared["transactions"][0]))
    prepared["preview"]["preparedOperations"].append({
        **prepared["preview"]["preparedOperations"][0], "txIndex": 1,
    })
    result = await run(runtime)
    assert len(result["transactions"]) == result["unsubmitted"] == 1
    assert runtime.rpc.sends == 1


@pytest.mark.parametrize("field,value", [
    ("gasLimit", 99999), ("gasEstimate", None), ("gasLimit", 100000000),
    ("sender", "0x" + "9" * 40), ("chainId", 2),
])
@pytest.mark.asyncio
async def test_invalid_intent_never_signs_or_sends(runtime, prepared, field, value):
    prepared["transactions"][0][field] = value
    result = await run(runtime)
    assert result["blockers"]
    assert runtime.signer.calls == runtime.rpc.sends == 0
    assert runtime.session.execute(select(models.transactions)).first() is None


@pytest.mark.asyncio
async def test_missing_or_conflicting_business_links_prevent_sending(runtime, prepared):
    prepared["preview"]["preparedOperations"][0]["operation"] = "kick"
    result = await run(runtime)
    assert result["blockers"][0]["code"] == "INVALID_INTENT"
    assert runtime.signer.calls == runtime.rpc.sends == 0


@pytest.mark.asyncio
async def test_declining_confirmation_sends_nothing(runtime, prepared):
    result = await run(runtime, confirm=lambda _: False)
    assert result["preparation_status"] == "skipped"
    assert runtime.signer.calls == runtime.rpc.sends == 0


@pytest.mark.asyncio
async def test_process_loss_retains_native_business_links(runtime, prepared):
    runtime.rpc.stop_after_commit = True
    with pytest.raises(KeyboardInterrupt):
        await run(runtime)
    rows = runtime.session.execute(select(models.transactions)).mappings().all()
    assert len(rows) == 1 and rows[0]["status"] == "RECORDED"
    assert runtime.session.execute(select(models.kick_txs)).mappings().all()
