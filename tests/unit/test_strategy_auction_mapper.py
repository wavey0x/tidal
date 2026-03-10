from eth_abi import encode as abi_encode
import pytest

from tidal.chain.contracts.multicall import MulticallResult
from tidal.scanner.auction_mapper import StrategyAuctionMapper


class FakeCall:
    def __init__(self, method: str, address: str | None = None):
        self.method = method
        self.address = address

    def _encode_transaction_data(self) -> str:
        return "0x00"


class FakeFactoryFunctions:
    def getAllAuctions(self) -> FakeCall:
        return FakeCall("factory.getAllAuctions")


class FakeAuctionFunctions:
    def __init__(self, address: str):
        self.address = address

    def want(self) -> FakeCall:
        return FakeCall("auction.want", self.address)

    def governance(self) -> FakeCall:
        return FakeCall("auction.governance", self.address)


class FakeStrategyFunctions:
    def __init__(self, address: str):
        self.address = address

    def want(self) -> FakeCall:
        return FakeCall("strategy.want", self.address)


class FakeContract:
    def __init__(self, kind: str, address: str | None = None):
        if kind == "factory":
            self.functions = FakeFactoryFunctions()
            return
        if kind == "auction":
            assert address is not None
            self.functions = FakeAuctionFunctions(address)
            return
        assert address is not None
        self.functions = FakeStrategyFunctions(address)


class FakeWeb3Client:
    def __init__(
        self,
        *,
        factory_address: str,
        auctions: list[str],
        auction_wants: dict[str, str],
        auction_governance: dict[str, str],
        strategy_wants: dict[str, str],
    ):
        self.factory_address = factory_address.lower()
        self.auctions = auctions
        self.auction_wants = {key.lower(): value for key, value in auction_wants.items()}
        self.auction_governance = {key.lower(): value for key, value in auction_governance.items()}
        self.strategy_wants = {key.lower(): value for key, value in strategy_wants.items()}

    def contract(self, address: str, abi):  # noqa: ANN001
        del abi
        lower = address.lower()
        if lower == self.factory_address:
            return FakeContract("factory")
        if lower in self.auction_wants:
            return FakeContract("auction", lower)
        return FakeContract("strategy", lower)

    async def call(self, call_fn: FakeCall):
        if call_fn.method == "factory.getAllAuctions":
            return self.auctions
        if call_fn.method == "auction.want":
            return self.auction_wants[call_fn.address]
        if call_fn.method == "auction.governance":
            return self.auction_governance[call_fn.address]
        if call_fn.method == "strategy.want":
            return self.strategy_wants[call_fn.address]
        raise RuntimeError(f"unknown call: {call_fn.method}")


class FakeMulticallClient:
    def __init__(self, responses: dict[tuple[str, ...], tuple[bool, bytes]]):
        self.responses = responses
        self.calls = 0

    async def execute(self, calls, *, batch_size, block="latest", allow_failure=True):  # noqa: ANN001
        del batch_size
        del block
        del allow_failure
        self.calls += 1
        return [
            MulticallResult(
                logical_key=call.logical_key,
                success=self.responses[call.logical_key][0],
                return_data=self.responses[call.logical_key][1],
            )
            for call in calls
        ]


@pytest.mark.asyncio
async def test_strategy_auction_mapper_uses_latest_factory_order_with_governance_filter() -> None:
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    other_governance = "0x1111111111111111111111111111111111111111"

    token_a = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    token_b = "0xd533a949740bb3306d119cc777fa900ba034cd52"

    auction_old = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    auction_wrong_governance = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    auction_new = "0xcccccccccccccccccccccccccccccccccccccccc"

    strategy_a = "0x1111111111111111111111111111111111111111"
    strategy_b = "0x2222222222222222222222222222222222222222"
    strategy_c = "0x3333333333333333333333333333333333333333"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction_old, auction_wrong_governance, auction_new],
            auction_wants={
                auction_old: token_a,
                auction_wrong_governance: token_b,
                auction_new: token_a,
            },
            auction_governance={
                auction_old: required_governance,
                auction_wrong_governance: other_governance,
                auction_new: required_governance,
            },
            strategy_wants={
                strategy_a: token_a,
                strategy_b: token_b,
                strategy_c: "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b",
            },
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_strategies([strategy_a, strategy_b, strategy_c])

    assert result.source == "fresh"
    assert result.auction_count == 3
    assert result.governance_allowed_auction_count == 2
    assert result.mapped_count == 1
    assert result.unmapped_count == 2
    assert result.strategy_to_auction == {
        strategy_a.lower(): auction_new.lower(),
        strategy_b.lower(): None,
        strategy_c.lower(): None,
    }


@pytest.mark.asyncio
async def test_strategy_auction_mapper_uses_multicall_for_auction_and_strategy_reads() -> None:
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    token_a = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

    auction_old = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    auction_new = "0xcccccccccccccccccccccccccccccccccccccccc"
    strategy = "0x1111111111111111111111111111111111111111"

    multicall = FakeMulticallClient(
        responses={
            (auction_old.lower(), "governance"): (True, abi_encode(["address"], [required_governance])),
            (auction_old.lower(), "want"): (True, abi_encode(["address"], [token_a])),
            (auction_new.lower(), "governance"): (True, abi_encode(["address"], [required_governance])),
            (auction_new.lower(), "want"): (True, abi_encode(["address"], [token_a])),
            (strategy.lower(),): (True, abi_encode(["address"], [token_a])),
        }
    )

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction_old, auction_new],
            auction_wants={auction_old: token_a, auction_new: token_a},
            auction_governance={auction_old: required_governance, auction_new: required_governance},
            strategy_wants={strategy: token_a},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
        multicall_client=multicall,
        multicall_enabled=True,
        multicall_auction_batch_calls=50,
    )

    result = await mapper.refresh_for_strategies([strategy])

    assert multicall.calls == 2
    assert result.strategy_to_auction == {
        strategy.lower(): auction_new.lower(),
    }
