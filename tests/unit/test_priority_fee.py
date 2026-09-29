"""Priority-fee suggestions must retain the fallback and operator cap."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.transaction_service.kick_shared import resolve_priority_fee_wei


@pytest.mark.asyncio
@pytest.mark.parametrize("suggestion,cap_gwei,expected", [
    (0, 2, 100000000),
    (-1, 2, 100000000),
    (None, 2, 100000000),
    (50000000, 2, 50000000),
    (3000000000, 2, 2000000000),
    (0, 0.05, 50000000),
    (0, 0, 0),
])
async def test_priority_fee_fallback_respects_suggestions_and_cap(suggestion, cap_gwei, expected):
    rpc = SimpleNamespace(get_max_priority_fee=AsyncMock(return_value=suggestion))
    if suggestion is None:
        rpc.get_max_priority_fee.side_effect = TimeoutError("RPC unavailable")
    assert await resolve_priority_fee_wei(rpc, cap_gwei) == expected
