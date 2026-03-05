import pytest

from tidal.chain.contracts.multicall import (
    MulticallClient,
    MulticallExecutionStats,
    MulticallRequest,
    MulticallResult,
)


class FakeMulticallClient(MulticallClient):
    def __init__(self, *, fail_first_chunk: bool = False):
        super().__init__(web3_client=None, multicall_address="0x0000000000000000000000000000000000000001", enabled=True)
        self.fail_first_chunk = fail_first_chunk
        self._multicall_calls = 0
        self._direct_calls = 0

    async def _execute_multicall_chunk(self, chunk, *, block, allow_failure):
        del block
        del allow_failure
        self._multicall_calls += 1
        if self.fail_first_chunk and self._multicall_calls == 1:
            raise RuntimeError("execution reverted")
        return [
            MulticallResult(
                logical_key=request.logical_key,
                success=True,
                return_data=b"\x01",
            )
            for request in chunk
        ]

    async def _execute_direct_chunk(self, chunk, *, block):
        del block
        self._direct_calls += len(chunk)
        return [
            MulticallResult(
                logical_key=request.logical_key,
                success=True,
                return_data=b"\x02",
                via_fallback=True,
            )
            for request in chunk
        ]


@pytest.mark.asyncio
async def test_multicall_client_keeps_order_and_stats_on_success() -> None:
    client = FakeMulticallClient()
    requests = [
        MulticallRequest(
            target="0x0000000000000000000000000000000000000001",
            call_data=b"a",
            logical_key=(str(i),),
        )
        for i in range(5)
    ]

    result = await client.execute(requests, batch_size=2)

    assert [item.logical_key for item in result] == [("0",), ("1",), ("2",), ("3",), ("4",)]
    assert all(item.via_fallback is False for item in result)
    assert client.last_stats == MulticallExecutionStats(
        batch_count=3,
        subcalls_total=5,
        subcalls_failed=0,
        fallback_direct_calls_total=0,
    )


@pytest.mark.asyncio
async def test_multicall_client_falls_back_and_disables_for_run() -> None:
    client = FakeMulticallClient(fail_first_chunk=True)
    requests = [
        MulticallRequest(
            target="0x0000000000000000000000000000000000000001",
            call_data=b"a",
            logical_key=(str(i),),
        )
        for i in range(5)
    ]

    result = await client.execute(requests, batch_size=2)

    assert all(item.via_fallback for item in result)
    assert client.last_stats.batch_count == 3
    assert client.last_stats.subcalls_total == 5
    assert client.last_stats.fallback_direct_calls_total == 5
