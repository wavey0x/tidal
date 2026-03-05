from eth_abi import encode as abi_encode
import pytest

from tidal.chain.contracts.erc20 import ERC20Reader
from tidal.chain.contracts.multicall import MulticallExecutionStats, MulticallResult


class FakeCall:
    def __init__(self):
        self.index = 0

    def _encode_transaction_data(self) -> str:
        return "0x1234"


class FakeTokenFunctions:
    def balanceOf(self, account):
        del account
        return FakeCall()


class FakeTokenContract:
    def __init__(self):
        self.functions = FakeTokenFunctions()


class FakeWeb3Client:
    def contract(self, address: str, abi):
        del address
        del abi
        return FakeTokenContract()


class FakeMulticallClient:
    def __init__(self):
        self.last_stats = MulticallExecutionStats(
            batch_count=1,
            subcalls_total=2,
            subcalls_failed=1,
            fallback_direct_calls_total=0,
        )

    async def execute(self, calls, *, batch_size, block="latest", allow_failure=True):
        del batch_size
        del block
        del allow_failure
        return [
            MulticallResult(
                logical_key=calls[0].logical_key,
                success=True,
                return_data=abi_encode(["uint256"], [123]),
            ),
            MulticallResult(
                logical_key=calls[1].logical_key,
                success=False,
                return_data=b"",
            ),
        ]


@pytest.mark.asyncio
async def test_read_balances_many_decodes_uint256_and_keeps_failures() -> None:
    reader = ERC20Reader(
        FakeWeb3Client(),
        multicall_client=FakeMulticallClient(),
        multicall_enabled=True,
        multicall_balance_batch_calls=10,
    )

    pairs = [
        ("0x1111111111111111111111111111111111111111", "0xd533a949740bb3306d119cc777fa900ba034cd52"),
        ("0x2222222222222222222222222222222222222222", "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b"),
    ]

    values, stats = await reader.read_balances_many(pairs)

    assert values[(pairs[0][0], pairs[0][1])] == 123
    assert values[(pairs[1][0], pairs[1][1])] is None
    assert stats["subcalls_total"] == 2
    assert stats["subcalls_failed"] == 1
