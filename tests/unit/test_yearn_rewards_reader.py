from eth_abi import encode as abi_encode
import pytest

from tidal.chain.contracts.multicall import MulticallExecutionStats, MulticallResult
from tidal.chain.contracts.yearn import StrategyRewardsReader


class FakeIndexedCall:
    def __init__(self, index: int):
        self.index = index

    def _encode_transaction_data(self) -> str:
        return f"0x{self.index:064x}"


class FakeStrategyContract:
    def get_function_by_signature(self, signature: str):
        assert signature == "rewardsTokens(uint256)"

        def _factory(index: int) -> FakeIndexedCall:
            return FakeIndexedCall(index)

        return _factory


class FakeWeb3Client:
    def __init__(self, direct_values: dict[int, str] | None = None):
        self.direct_values = direct_values or {}

    def contract(self, address: str, abi):
        del address
        del abi
        return FakeStrategyContract()

    async def call(self, call_fn):
        value = self.direct_values.get(call_fn.index)
        if value is None:
            raise RuntimeError("out of bounds")
        return value


class FakeMulticallClient:
    def __init__(self, responses: dict[tuple[str, str], tuple[bool, bytes]], *, total_calls: int):
        self.responses = responses
        self.last_stats = MulticallExecutionStats(
            batch_count=1,
            subcalls_total=total_calls,
            subcalls_failed=sum(1 for success, _ in responses.values() if not success),
            fallback_direct_calls_total=0,
        )

    async def execute(self, calls, *, batch_size, block="latest", allow_failure=True):
        del batch_size
        del block
        del allow_failure

        results: list[MulticallResult] = []
        for call in calls:
            success, return_data = self.responses[call.logical_key]
            results.append(
                MulticallResult(
                    logical_key=call.logical_key,
                    success=success,
                    return_data=return_data,
                )
            )
        return results


@pytest.mark.asyncio
async def test_rewards_tokens_many_treats_first_failure_as_termination() -> None:
    strategy = "0x1111111111111111111111111111111111111111"
    responses = {
        (strategy, "0"): (False, b""),
        (strategy, "1"): (False, b""),
        (strategy, "2"): (False, b""),
    }

    reader = StrategyRewardsReader(
        FakeWeb3Client(),
        multicall_client=FakeMulticallClient(responses, total_calls=3),
        multicall_enabled=True,
        multicall_rewards_batch_calls=10,
        multicall_rewards_index_max=3,
    )

    resolved, stats = await reader.rewards_tokens_many([strategy])

    assert resolved[strategy] == []
    assert stats["subcalls_total"] == 3


@pytest.mark.asyncio
async def test_rewards_tokens_many_reads_indexed_tokens_until_failure() -> None:
    strategy = "0x2222222222222222222222222222222222222222"
    token_a = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    token_b = "0xC02aaA39b223FE8D0a0e5C4F27eAD9083C756Cc2"
    responses = {
        (strategy, "0"): (True, abi_encode(["address"], [token_a])),
        (strategy, "1"): (True, abi_encode(["address"], [token_b])),
        (strategy, "2"): (False, b""),
        (strategy, "3"): (True, abi_encode(["address"], [token_a])),
    }

    reader = StrategyRewardsReader(
        FakeWeb3Client(),
        multicall_client=FakeMulticallClient(responses, total_calls=4),
        multicall_enabled=True,
        multicall_rewards_batch_calls=10,
        multicall_rewards_index_max=4,
    )

    resolved, _ = await reader.rewards_tokens_many([strategy])

    assert resolved[strategy] == [
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    ]


@pytest.mark.asyncio
async def test_rewards_tokens_direct_stops_when_index_read_fails() -> None:
    reader = StrategyRewardsReader(
        FakeWeb3Client(
            direct_values={
                0: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                1: "0xC02aaA39b223FE8D0a0e5C4F27eAD9083C756Cc2",
            }
        ),
        multicall_enabled=False,
        multicall_rewards_index_max=5,
    )

    resolved = await reader.rewards_tokens("0x3333333333333333333333333333333333333333")

    assert resolved == [
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    ]
