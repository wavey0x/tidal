from eth_abi import encode as abi_encode
import pytest

from factory_dashboard.chain.contracts.multicall import MulticallResult
from factory_dashboard.scanner.auction_mapper import StrategyAuctionMapper


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

    def receiver(self) -> FakeCall:
        return FakeCall("auction.receiver", self.address)

    def version(self) -> FakeCall:
        return FakeCall("auction.version", self.address)


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
        auction_receivers: dict[str, str],
        auction_versions: dict[str, str] | None = None,
        strategy_wants: dict[str, str],
    ):
        self.factory_address = factory_address.lower()
        self.auctions = auctions
        self.auction_wants = {key.lower(): value for key, value in auction_wants.items()}
        self.auction_governance = {key.lower(): value for key, value in auction_governance.items()}
        self.auction_receivers = {key.lower(): value for key, value in auction_receivers.items()}
        self.auction_versions = {key.lower(): value for key, value in (auction_versions or {}).items()}
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
        if call_fn.method == "auction.receiver":
            return self.auction_receivers[call_fn.address]
        if call_fn.method == "auction.version":
            if call_fn.address not in self.auction_versions:
                raise RuntimeError("version not available")
            return self.auction_versions[call_fn.address]
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
async def test_strategy_auction_mapper_uses_latest_factory_order_with_governance_and_receiver() -> None:
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
            auction_receivers={
                auction_old: strategy_a,
                auction_wrong_governance: strategy_b,
                auction_new: strategy_a,
            },
            auction_versions={
                auction_old: "1.0.0",
                auction_new: "1.0.1",
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
    assert result.valid_auction_count == 2
    assert result.receiver_filtered_count == 0
    assert result.mapped_count == 1
    assert result.unmapped_count == 2
    assert result.strategy_to_auction == {
        strategy_a.lower(): auction_new.lower(),
        strategy_b.lower(): None,
        strategy_c.lower(): None,
    }
    assert result.strategy_to_auction_version == {
        strategy_a.lower(): "1.0.1",
        strategy_b.lower(): None,
        strategy_c.lower(): None,
    }


@pytest.mark.asyncio
async def test_strategy_auction_mapper_requires_receiver_match() -> None:
    """Auction with correct governance and want but mismatched receiver is not mapped."""
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    token_a = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

    auction = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    other_receiver = "0x9999999999999999999999999999999999999999"
    strategy = "0x1111111111111111111111111111111111111111"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction],
            auction_wants={auction: token_a},
            auction_governance={auction: required_governance},
            auction_receivers={auction: other_receiver},
            auction_versions={auction: "1.0.0"},
            strategy_wants={strategy: token_a},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_strategies([strategy])

    assert result.valid_auction_count == 1
    assert result.receiver_filtered_count == 0
    assert result.mapped_count == 0
    assert result.strategy_to_auction[strategy.lower()] is None
    assert result.strategy_to_auction_version[strategy.lower()] is None


@pytest.mark.asyncio
async def test_strategy_auction_mapper_version_failure_still_maps() -> None:
    """version() failure does not prevent auction matching."""
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    token_a = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

    auction = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    strategy = "0x1111111111111111111111111111111111111111"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction],
            auction_wants={auction: token_a},
            auction_governance={auction: required_governance},
            auction_receivers={auction: strategy},
            # No auction_versions — version() will raise
            strategy_wants={strategy: token_a},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_strategies([strategy])

    assert result.mapped_count == 1
    assert result.strategy_to_auction[strategy.lower()] == auction.lower()
    assert result.strategy_to_auction_version[strategy.lower()] is None


@pytest.mark.asyncio
async def test_strategy_auction_mapper_receiver_filtered_count() -> None:
    """Auctions with None/zero receiver are counted as receiver-filtered."""
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    token_a = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

    auction_no_receiver = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    auction_zero_receiver = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    strategy = "0x1111111111111111111111111111111111111111"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction_no_receiver, auction_zero_receiver],
            auction_wants={auction_no_receiver: token_a, auction_zero_receiver: token_a},
            auction_governance={
                auction_no_receiver: required_governance,
                auction_zero_receiver: required_governance,
            },
            auction_receivers={
                auction_no_receiver: "0x0000000000000000000000000000000000000000",
                auction_zero_receiver: "0x0000000000000000000000000000000000000000",
            },
            strategy_wants={strategy: token_a},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_strategies([strategy])

    assert result.valid_auction_count == 0
    assert result.receiver_filtered_count == 2
    assert result.mapped_count == 0


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
            (auction_old.lower(), "receiver"): (True, abi_encode(["address"], [strategy])),
            (auction_old.lower(), "version"): (True, abi_encode(["string"], ["1.0.0"])),
            (auction_new.lower(), "governance"): (True, abi_encode(["address"], [required_governance])),
            (auction_new.lower(), "want"): (True, abi_encode(["address"], [token_a])),
            (auction_new.lower(), "receiver"): (True, abi_encode(["address"], [strategy])),
            (auction_new.lower(), "version"): (True, abi_encode(["string"], ["1.0.1"])),
            (strategy.lower(),): (True, abi_encode(["address"], [token_a])),
        }
    )

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction_old, auction_new],
            auction_wants={auction_old: token_a, auction_new: token_a},
            auction_governance={auction_old: required_governance, auction_new: required_governance},
            auction_receivers={auction_old: strategy, auction_new: strategy},
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
    assert result.strategy_to_auction_version == {
        strategy.lower(): "1.0.1",
    }


@pytest.mark.asyncio
async def test_fee_burner_auction_mapper_matches_configured_want_and_receiver() -> None:
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    burner = "0xb911fcce8d5afcec73e072653107260bb23c1ee8"
    want = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
    other_want = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    matching_auction = "0x10bd77b0aa255d5cb7db1273705c1f0568536035"
    wrong_want_auction = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[wrong_want_auction, matching_auction],
            auction_wants={wrong_want_auction: other_want, matching_auction: want},
            auction_governance={wrong_want_auction: required_governance, matching_auction: required_governance},
            auction_receivers={wrong_want_auction: burner, matching_auction: burner},
            auction_versions={matching_auction: "1.0.3cc"},
            strategy_wants={},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_fee_burners({burner: want})

    assert result.mapped_count == 1
    assert result.unmapped_count == 0
    assert result.fee_burner_to_auction[burner.lower()] == matching_auction.lower()
    assert result.fee_burner_to_want[burner.lower()] == want.lower()
    assert result.fee_burner_to_auction_version[burner.lower()] == "1.0.3cc"
    assert result.fee_burner_to_error == {}


@pytest.mark.asyncio
async def test_fee_burner_auction_mapper_fails_closed_on_multiple_matches() -> None:
    factory = "0xe87af17acba165686e5aa7de2cec523864c25712"
    required_governance = "0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b"
    burner = "0xb911fcce8d5afcec73e072653107260bb23c1ee8"
    want = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
    auction_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    auction_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    mapper = StrategyAuctionMapper(
        web3_client=FakeWeb3Client(
            factory_address=factory,
            auctions=[auction_a, auction_b],
            auction_wants={auction_a: want, auction_b: want},
            auction_governance={auction_a: required_governance, auction_b: required_governance},
            auction_receivers={auction_a: burner, auction_b: burner},
            strategy_wants={},
        ),
        chain_id=1,
        auction_factory_address=factory,
        required_governance_address=required_governance,
    )

    result = await mapper.refresh_for_fee_burners({burner: want})

    assert result.mapped_count == 0
    assert result.unmapped_count == 1
    assert result.fee_burner_to_auction[burner.lower()] is None
    assert result.fee_burner_to_error[burner.lower()] == "multiple matching auctions found for configured want/receiver"
