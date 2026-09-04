"""Keep request-owned reads and cleanup within their client's lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


async def _finish_cleanup(awaitable: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def close_client(client) -> None:  # noqa: ANN001
    """Finish closing in this loop before propagating request cancellation."""
    await _finish_cleanup(client.close())


async def gather_reads(*reads: Awaitable[Any]) -> list[Any]:
    """Preserve gather's results/errors, but never leave sibling reads running."""
    tasks = [asyncio.ensure_future(read) for read in reads]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await _finish_cleanup(asyncio.gather(*tasks, return_exceptions=True))
