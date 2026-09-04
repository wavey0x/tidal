import asyncio
from threading import BoundedSemaphore, Event, get_ident
from types import SimpleNamespace

import httpx
import pytest

from tidal.api.app import create_app
from tidal.api.errors import APIError
from tidal.api.services import action_prepare, deployment_rpc
from tidal.config import Settings


@pytest.fixture(autouse=True)
def isolated_slots(monkeypatch):
    monkeypatch.setattr(deployment_rpc, "_slots", BoundedSemaphore(4))


async def test_slow_deploy_requests_do_not_block_health_and_excess_work_is_rejected(monkeypatch, tmp_path):
    release = Event()
    started = [Event() for _ in range(4)]
    owner_thread = get_ident()

    def preview(settings, **kwargs):
        assert get_ident() != owner_thread
        index = int(kwargs["starting_price"]) - 1
        started[index].set()
        assert release.wait(5), "test did not release preview"
        return [], {}, {}, {"to": kwargs["receiver"]}

    monkeypatch.setattr(action_prepare, "_build_deploy_prepare_payload", preview)
    app = create_app(Settings(db_path=tmp_path / "api.db", rpc_url=""))
    payload = {"want": "0x" + "11" * 20, "receiver": "0x" + "22" * 20}
    path = "/api/v1/tidal/auctions/deploy/browser-prepare"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        requests = [asyncio.create_task(client.post(path, json={**payload, "startingPrice": i + 1})) for i in range(4)]
        try:
            for event in started:
                assert await asyncio.to_thread(event.wait, 2)
            health = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert health.status_code == 200
            busy = await asyncio.wait_for(client.post(path, json={**payload, "startingPrice": 5}), timeout=0.5)
            assert busy.status_code == 503
            assert "retry" in busy.json()["detail"]
        finally:
            release.set()
            responses = await asyncio.gather(*requests)
        assert all(response.status_code == 200 for response in responses)


async def test_cancellation_keeps_admission_until_rpc_finishes():
    started = [Event() for _ in range(4)]
    release = Event()

    def slow(index):
        started[index].set()
        assert release.wait(5)
        return index

    tasks = [asyncio.create_task(deployment_rpc.run_deployment_rpc(slow, i)) for i in range(4)]
    try:
        for event in started:
            assert await asyncio.to_thread(event.wait, 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        with pytest.raises(APIError) as busy:
            await deployment_rpc.run_deployment_rpc(lambda: None)
        assert busy.value.status_code == 503
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert await deployment_rpc.run_deployment_rpc(lambda: "recovered") == "recovered"


async def test_worker_failure_releases_admission():
    def fail():
        raise ValueError("fixture failure")

    for _ in range(5):
        with pytest.raises(ValueError, match="fixture failure"):
            await deployment_rpc.run_deployment_rpc(fail)
    assert await deployment_rpc.run_deployment_rpc(lambda: 1) == 1


async def test_operator_action_is_recorded_in_request_context(monkeypatch):
    owner_thread = get_ident()
    session = object()

    def preview(settings, **kwargs):
        assert get_ident() != owner_thread
        return [], {"receiver": "0x" + "22" * 20}, {"predictedAuctionAddress": None}, {}

    def record(received_session, **kwargs):
        assert get_ident() == owner_thread
        assert received_session is session
        return "action-1"

    monkeypatch.setattr(action_prepare, "_build_deploy_prepare_payload", preview)
    monkeypatch.setattr(action_prepare, "create_prepared_action", record)
    status, _, data = await action_prepare.prepare_deploy_action(
        SimpleNamespace(), session, operator_id="operator", want="0x" + "11" * 20,
        receiver="0x" + "22" * 20, sender=None, factory=None, governance=None,
        starting_price=1, salt=None,
    )
    assert status == "ok"
    assert data["actionId"] == "action-1"


async def test_missing_rpc_is_an_api_error_not_system_exit():
    with pytest.raises(APIError) as error:
        await action_prepare.prepare_deploy_browser_action(
            SimpleNamespace(rpc_url=""), want="0x" + "11" * 20,
            receiver="0x" + "22" * 20, sender=None, factory=None, governance=None,
            starting_price=1, salt=None,
        )
    assert error.value.status_code == 503
