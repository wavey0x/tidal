import pytest

from tidal.chain.contracts.yearn import StrategyGaugeStatusReader


class _FakeCall:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeFunctions:
    def gauge(self) -> _FakeCall:
        return _FakeCall("gauge")

    def is_killed(self) -> _FakeCall:
        return _FakeCall("is_killed")


class _FakeContract:
    functions = _FakeFunctions()


class _FakeWeb3Client:
    def __init__(
        self,
        *,
        gauge_address: str | None,
        killed: bool | None,
    ) -> None:
        self.gauge_address = gauge_address
        self.killed = killed

    def contract(self, address: str, abi):  # noqa: ANN001
        del address, abi
        return _FakeContract()

    async def call(self, call_fn: _FakeCall):  # noqa: ANN001
        if call_fn.name == "gauge":
            if self.gauge_address is None:
                raise RuntimeError("gauge unavailable")
            return self.gauge_address
        if call_fn.name == "is_killed":
            if self.killed is None:
                raise RuntimeError("is_killed unavailable")
            return self.killed
        raise AssertionError(f"unexpected call {call_fn.name}")


@pytest.mark.asyncio
@pytest.mark.parametrize(("killed", "expected"), [(True, True), (False, False)])
async def test_strategy_gauge_status_reader_reads_is_killed(killed: bool, expected: bool) -> None:
    reader = StrategyGaugeStatusReader(
        _FakeWeb3Client(
            gauge_address="0x2222222222222222222222222222222222222222",
            killed=killed,
        )
    )

    assert await reader.is_killed("0x1111111111111111111111111111111111111111") is expected


@pytest.mark.asyncio
async def test_strategy_gauge_status_reader_returns_none_when_gauge_unreadable() -> None:
    reader = StrategyGaugeStatusReader(_FakeWeb3Client(gauge_address=None, killed=True))

    assert await reader.is_killed("0x1111111111111111111111111111111111111111") is None


@pytest.mark.asyncio
async def test_strategy_gauge_status_reader_returns_none_when_is_killed_unreadable() -> None:
    reader = StrategyGaugeStatusReader(
        _FakeWeb3Client(
            gauge_address="0x2222222222222222222222222222222222222222",
            killed=None,
        )
    )

    assert await reader.is_killed("0x1111111111111111111111111111111111111111") is None
