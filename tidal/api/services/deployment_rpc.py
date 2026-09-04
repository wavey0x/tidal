"""Fail-fast admission for synchronous deployment reads in the API thread pool."""

from __future__ import annotations

import asyncio
from threading import BoundedSemaphore
from typing import Callable, ParamSpec, TypeVar

from starlette.concurrency import run_in_threadpool

from tidal.api.errors import APIError

_slots = BoundedSemaphore(4)
_inflight: set[asyncio.Task] = set()
P = ParamSpec("P")
T = TypeVar("T")


async def run_deployment_rpc(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    slots = _slots
    if not slots.acquire(blocking=False):
        raise APIError("Deployment previews are busy; retry shortly", status_code=503)

    def run() -> T:
        try:
            return operation(*args, **kwargs)
        finally:
            # A disconnected request must not free capacity while its RPC runs.
            slots.release()

    task = asyncio.create_task(run_in_threadpool(run))
    _inflight.add(task)
    # Keep admitted work alive if its caller is cancelled, and observe any error
    # even when there is no longer a request waiting for the result.
    def completed(done: asyncio.Task) -> None:
        _inflight.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)
    return await asyncio.shield(task)
