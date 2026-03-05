"""Retry helpers for transient RPC errors."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from web3.exceptions import ContractLogicError


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, ContractLogicError):
        return False

    message = str(exc).lower()
    non_retryable_patterns = (
        "execution reverted",
        "invalid opcode",
        "abi",
        "invalid address",
    )
    if any(pattern in message for pattern in non_retryable_patterns):
        return False

    retryable_patterns = (
        "timeout",
        "temporarily unavailable",
        "connection",
        "gateway",
        "429",
        "rate limit",
    )
    if any(pattern in message for pattern in retryable_patterns):
        return True

    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


async def call_with_retries(
    fn: Callable[[], Awaitable[object]],
    *,
    attempts: int,
    base_delay_seconds: float = 0.25,
) -> object:
    """Execute an async function with bounded retries on transient errors."""

    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= attempts or not is_retryable_error(exc):
                raise

            backoff = base_delay_seconds * (2 ** (attempt - 1))
            jitter = random.uniform(0, base_delay_seconds)
            await asyncio.sleep(backoff + jitter)

    if last_error is not None:
        raise last_error

    raise RuntimeError("unreachable retry state")
