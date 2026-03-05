from eth_abi import encode as abi_encode
import pytest

from tidal.chain.contracts.multicall import MulticallExecutionStats, MulticallResult
from tidal.chain.contracts.yearn import YearnCurveFactoryReader
from tidal.constants import ZERO_ADDRESS


class FakeCall:
    def __init__(self, index: int):
        self.index = index

    def _encode_transaction_data(self) -> str:
        return f"0x{self.index:064x}"


class FakeVaultFunctions:
    def withdrawalQueue(self, index: int) -> FakeCall:
        return FakeCall(index)


class FakeVaultContract:
    def __init__(self):
        self.functions = FakeVaultFunctions()


class FakeWeb3Client:
    def contract(self, address: str, abi):
        del address
        del abi
        return FakeVaultContract()

    async def call(self, call_fn):
        # Overflow fallback reads index 4 and receives zero sentinel.
        if call_fn.index == 4:
            return ZERO_ADDRESS
        raise RuntimeError("unexpected direct fallback call")


class FakeMulticallClient:
    def __init__(self):
        self.last_stats = MulticallExecutionStats(
            batch_count=1,
            subcalls_total=8,
            subcalls_failed=0,
            fallback_direct_calls_total=0,
        )

    async def execute(self, calls, *, batch_size, block="latest", allow_failure=True):
        del batch_size
        del block
        del allow_failure

        results = []
        for call in calls:
            vault, idx = call.logical_key

            if vault == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
                if idx == "0":
                    address = "0x1111111111111111111111111111111111111111"
                else:
                    address = ZERO_ADDRESS
                results.append(
                    MulticallResult(
                        logical_key=call.logical_key,
                        success=True,
                        return_data=abi_encode(["address"], [address]),
                    )
                )
                continue

            # vault b has four entries -> overflow candidate.
            if idx == "0":
                address = "0x2222222222222222222222222222222222222221"
            elif idx == "1":
                address = "0x2222222222222222222222222222222222222222"
            elif idx == "2":
                address = "0x2222222222222222222222222222222222222223"
            else:
                address = "0x2222222222222222222222222222222222222224"

            results.append(
                MulticallResult(
                    logical_key=call.logical_key,
                    success=True,
                    return_data=abi_encode(["address"], [address]),
                )
            )

        return results


@pytest.mark.asyncio
async def test_strategies_for_vaults_batched_parses_fixed_window_and_overflow() -> None:
    reader = YearnCurveFactoryReader(
        FakeWeb3Client(),
        "0x21b1fc8a52f179757bf555346130bf27c0c2a17a",
        multicall_client=FakeMulticallClient(),
        multicall_enabled=True,
        multicall_discovery_batch_calls=64,
        multicall_overflow_queue_max=5,
    )

    mapping, stats = await reader.strategies_for_vaults_batched(
        [
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ]
    )

    assert mapping["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] == [
        "0x1111111111111111111111111111111111111111"
    ]
    assert mapping["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"] == [
        "0x2222222222222222222222222222222222222221",
        "0x2222222222222222222222222222222222222222",
        "0x2222222222222222222222222222222222222223",
        "0x2222222222222222222222222222222222222224",
    ]
    assert stats["overflow_vaults_count"] == 1
    assert stats["fallback_direct_calls_total"] == 1
