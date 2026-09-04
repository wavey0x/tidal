import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from tidal import runtime
from tidal.async_resources import close_client, gather_reads
from tidal.api.errors import APIError
from tidal.api.services import action_prepare
from tidal.auction_versions import AUCTION_V105_FACTORY_ADDRESS
from tidal.config import Settings
from tidal.resources import read_template_text
from tidal.ops import kick_inspect
from tidal.transaction_service.planner import KickPlanner
from tidal.transaction_service.kick_policy import build_kick_config
from tidal.transaction_service.kick_prepare import KickPreparer
from tidal.transaction_service.types import AuctionInspection, KickCandidate

AUCTION = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20


class TrackedClient:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.closed = 0
        self.contract = MagicMock()

    async def close(self):
        assert asyncio.get_running_loop() is self.loop
        await asyncio.sleep(0)
        self.closed += 1


async def phase(outcome, result):
    if outcome == "error":
        raise RuntimeError("fixture failure")
    if outcome == "cancel":
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
    return result


async def check_outcome(coroutine, outcome):
    # Run cancellation in its own request task, leaving the test task untouched.
    task = asyncio.create_task(coroutine)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome in {"error", "write_error"}:
        with pytest.raises(RuntimeError, match="fixture failure"):
            await task
    else:
        result = await task
        assert result[0] == ("noop" if outcome == "noop" else "ok")


def settings():
    result = Settings(rpc_url="http://offline.test", txn_keystore_path=None, txn_keystore_passphrase=None)
    result.bind_kick_config(build_kick_config(yaml.safe_load(read_template_text("server.yaml"))["kick"]))
    return result


@pytest.mark.parametrize("outcome", ["success", "noop", "error", "cancel", "write_error"])
async def test_kick_prepare_closes_factory_owned_rpc_and_pricing_clients(monkeypatch, outcome):
    clients = []

    def build(*args, **kwargs):
        client = TrackedClient()
        clients.append(client)
        return client

    monkeypatch.setattr(runtime, "build_web3_client", build)
    monkeypatch.setattr("tidal.pricing.token_price_agg.TokenPriceAggProvider", build)
    plan = SimpleNamespace(
        to_preview_payload=lambda: {},
        to_transaction_payloads=lambda: [] if outcome == "noop" else [{}],
        warnings=[], status=lambda: "noop",
    )

    async def plan_kick(self, **kwargs):
        return await phase(outcome, plan)

    def record(*args, **kwargs):
        if outcome == "write_error":
            raise RuntimeError("fixture failure")
        return "action-1"

    monkeypatch.setattr(KickPlanner, "plan", plan_kick)
    monkeypatch.setattr(action_prepare, "create_prepared_action", record)
    await check_outcome(action_prepare.prepare_kick_action(
        object(), settings(), operator_id="test", source_type=None, source_address=None,
        auction_address=None, token_address=None, limit=1, sender=None,
    ), outcome)
    assert len(clients) == 2
    assert all(client.closed == 1 for client in clients)


async def test_factory_does_not_take_ownership_of_borrowed_rpc_client(monkeypatch):
    borrowed = TrackedClient()
    pricing = TrackedClient()
    monkeypatch.setattr("tidal.pricing.token_price_agg.TokenPriceAggProvider", lambda **kwargs: pricing)
    async with AsyncExitStack() as owned:
        runtime.build_txn_service(settings(), object(), web3_client=borrowed, owned_clients=owned)
    assert borrowed.closed == 0
    assert pricing.closed == 1


async def test_partial_factory_failure_closes_already_created_client(monkeypatch):
    rpc = TrackedClient()
    monkeypatch.setattr(runtime, "build_web3_client", lambda settings: rpc)

    def fail(**kwargs):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr("tidal.pricing.token_price_agg.TokenPriceAggProvider", fail)
    with pytest.raises(RuntimeError, match="fixture failure"):
        async with AsyncExitStack() as owned:
            runtime.build_txn_service(settings(), object(), owned_clients=owned)
    assert rpc.closed == 1


@pytest.mark.parametrize("operation", ["settle", "sweep"])
@pytest.mark.parametrize("outcome", ["success", "noop", "error", "cancel", "write_error"])
async def test_settle_and_sweep_close_owned_rpc_on_every_exit(monkeypatch, operation, outcome):
    rpc = TrackedClient()
    preview = SimpleNamespace(read_ok=True, balance_raw=0 if outcome == "noop" else 1, path=5, receiver=AUCTION)
    inspection = SimpleNamespace(preview_for_token=lambda token: preview)
    decision = SimpleNamespace(status="noop" if outcome == "noop" else "actionable", operations=[], reason="fixture")
    call = SimpleNamespace(token_address=TOKEN, target_address=AUCTION, data="0x00", operation_type="sweep_auction")

    async def inspect(*args, **kwargs):
        return await phase(outcome, inspection)

    def record(*args, **kwargs):
        if outcome == "write_error":
            raise RuntimeError("fixture failure")
        return "action-1"

    monkeypatch.setattr(action_prepare, "build_web3_client", lambda settings: rpc)
    monkeypatch.setattr(action_prepare, "inspect_auction_settlement", inspect)
    monkeypatch.setattr(action_prepare, "decide_auction_settlement", lambda *args, **kwargs: decision)
    monkeypatch.setattr(action_prepare, "build_auction_settlement_calls", lambda **kwargs: [call])
    monkeypatch.setattr(action_prepare, "build_auction_sweep_call", lambda **kwargs: call)
    monkeypatch.setattr(action_prepare, "_estimate_transaction", AsyncMock(return_value=(1, 1, None)))
    monkeypatch.setattr(action_prepare, "TokenRepository", lambda session: SimpleNamespace(get=lambda token: None))
    monkeypatch.setattr(action_prepare, "create_prepared_action", record)
    prepare = action_prepare.prepare_settle_action if operation == "settle" else action_prepare.prepare_sweep_action
    kwargs = {"force": False} if operation == "settle" else {}
    await check_outcome(prepare(settings(), object(), operator_id="test", auction_address=AUCTION,
                                sender=None, token_address=TOKEN, **kwargs), outcome)
    assert rpc.closed == 1


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "missing_decimals"])
async def test_deploy_defaults_close_quote_provider(monkeypatch, outcome):
    provider = TrackedClient()
    quote = SimpleNamespace(amount_out_raw=1_000_000, token_out_decimals=None if outcome == "missing_decimals" else 6,
                            request_url="http://offline.test", provider_statuses={}, curve_quote_available=lambda: False)

    async def get_quote(**kwargs):
        return await phase(outcome, quote)

    provider.quote = get_quote
    rows = [{"strategy_address": AUCTION, "strategy_name": "fixture", "auction_address": None,
             "want_address": TOKEN, "want_symbol": "USDC", "active": True,
             "token_address": "0x" + "33" * 20, "raw_balance": "1000000", "normalized_balance": "1",
             "token_symbol": "REWARD", "token_decimals": 6, "token_price_usd": "1"}]
    session = SimpleNamespace(execute=lambda *args: SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows)))
    monkeypatch.setattr(action_prepare, "TokenPriceAggProvider", lambda **kwargs: provider)
    monkeypatch.setattr(action_prepare, "build_sync_web3", lambda settings: object())
    monkeypatch.setattr(action_prepare, "default_factory_address", lambda settings: AUCTION_V105_FACTORY_ADDRESS)
    monkeypatch.setattr(action_prepare, "read_token_decimals", lambda *args: 6)
    monkeypatch.setattr(action_prepare, "preview_deployment", lambda *args, **kwargs: SimpleNamespace(
        predicted_address=None, predicted_address_exists=False, existing_matches=[],
    ))
    task = asyncio.create_task(action_prepare.load_strategy_deploy_defaults(session, settings(), strategy_address=AUCTION))
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "error":
        with pytest.raises(RuntimeError, match="fixture failure"):
            await task
    elif outcome == "missing_decimals":
        with pytest.raises(APIError, match="missing output token decimals"):
            await task
    else:
        assert (await task)["strategyAddress"] == AUCTION
    assert provider.closed == 1


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_live_inspection_creates_and_closes_client_inside_its_run_loop(monkeypatch, outcome):
    clients = []

    def build(settings):
        client = TrackedClient()
        clients.append(client)
        return client

    async def inspect(client, *args):
        assert client.loop is asyncio.get_running_loop()
        return await phase(outcome, {})

    candidate = SimpleNamespace(auction_address=AUCTION)
    shortlist = SimpleNamespace(selected_candidates=[candidate], deferred_same_auction_candidates=[], limited_candidates=[])
    monkeypatch.setattr(kick_inspect, "build_shortlist", lambda *args, **kwargs: shortlist)
    monkeypatch.setattr(kick_inspect, "build_web3_client", build)
    monkeypatch.setattr(kick_inspect, "inspect_auction_settlements", inspect)
    # The successful helper result is checked directly; the public synchronous
    # caller is covered below for exceptions, plus the existing inspection tests.
    if outcome == "success":
        assert asyncio.run(kick_inspect._inspect_with_owned_client(settings(), [AUCTION])) == {}
    else:
        expected = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(expected):
            kick_inspect.inspect_kick_candidates(object(), settings())
    assert len(clients) == 1
    assert clients[0].closed == 1


@pytest.mark.parametrize("operation", ["settle", "sweep", "inspect"])
async def test_cancellation_during_close_waits_for_cleanup(monkeypatch, operation):
    started = asyncio.Event()
    release = asyncio.Event()
    closed = []

    async def close():
        started.set()
        await release.wait()
        closed.append(True)

    rpc = SimpleNamespace(close=close)
    if operation == "inspect":
        monkeypatch.setattr(kick_inspect, "build_web3_client", lambda settings: rpc)
        monkeypatch.setattr(kick_inspect, "inspect_auction_settlements", AsyncMock(return_value={}))
        coroutine = kick_inspect._inspect_with_owned_client(settings(), [AUCTION])
    else:
        monkeypatch.setattr(action_prepare, "build_web3_client", lambda settings: rpc)
        preview = SimpleNamespace(read_ok=True, balance_raw=0)
        monkeypatch.setattr(action_prepare, "inspect_auction_settlement", AsyncMock(return_value=SimpleNamespace(
            preview_for_token=lambda token: preview,
        )))
        monkeypatch.setattr(action_prepare, "decide_auction_settlement", lambda *args, **kwargs: SimpleNamespace(
            status="noop", operations=[], reason="fixture",
        ))
        prepare = action_prepare.prepare_settle_action if operation == "settle" else action_prepare.prepare_sweep_action
        kwargs = {"force": False} if operation == "settle" else {}
        coroutine = prepare(settings(), object(), operator_id="test", auction_address=AUCTION,
                            sender=None, token_address=TOKEN, **kwargs)
    task = asyncio.create_task(coroutine)
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


async def test_failed_token_read_drains_siblings_before_owned_client_closes():
    siblings = []
    drained = []
    rpc = TrackedClient()

    async def fail_balance(*args):
        await asyncio.sleep(0)
        raise RuntimeError("fixture read failed")

    async def delayed_decimals(*args):
        siblings.append(asyncio.current_task())
        try:
            await asyncio.Future()
        finally:
            assert rpc.closed == 0
            drained.append(True)

    preparer = KickPreparer(
        web3_client=rpc, price_provider=object(), usd_threshold=0,
        erc20_reader=SimpleNamespace(read_balance=fail_balance, read_decimals=delayed_decimals),
        start_price_buffer_bps=1000, min_price_buffer_bps=50,
    )
    candidate = KickCandidate(source_type="strategy", source_address=AUCTION, token_address=TOKEN,
                              auction_address=AUCTION, want_address="0x" + "33" * 20, normalized_balance="1",
                              price_usd="1", usd_value=1, decimals=18, auction_version="1.0.5")
    inspection = AuctionInspection(auction_address=AUCTION, is_active_auction=False, active_tokens=(),
                                   auction_version="1.0.5", auction_length_seconds=86400, step_duration_seconds=60)
    try:
        result = await preparer.prepare_kick(candidate, "fixture", inspection=inspection)
        assert "live token read failed" in result.error_message
        assert drained == [True, True]
        assert all(task.done() for task in siblings)
        await rpc.close()
    finally:
        for task in siblings:
            task.cancel()
        await asyncio.gather(*siblings, return_exceptions=True)


async def test_owned_cleanup_finishes_despite_repeated_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()
    closed = []

    async def close():
        started.set()
        await release.wait()
        closed.append(True)

    task = asyncio.create_task(close_client(SimpleNamespace(close=close)))
    await started.wait()
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


async def test_read_group_preserves_results_and_drains_on_cancellation():
    assert await gather_reads(asyncio.sleep(0, result=1), asyncio.sleep(0, result=2)) == [1, 2]
    started = asyncio.Event()
    drained = []

    async def read():
        started.set()
        try:
            await asyncio.Future()
        finally:
            drained.append(True)

    task = asyncio.create_task(gather_reads(read(), read()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained == [True, True]
