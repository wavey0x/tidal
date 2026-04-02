import pytest

from tidal.config import MonitoredFeeBurner
from tidal.ops.auction_enable import AuctionInspection, AuctionTokenEnabler, parse_manual_token_input, resolve_source_type


class _FakeEnableFn:
    def __init__(self, data: str = "0xdeadbeef", call_error: Exception | None = None, gas_error: Exception | None = None) -> None:
        self._data = data
        self._call_error = call_error
        self._gas_error = gas_error

    def _encode_transaction_data(self) -> str:
        return self._data

    def call(self, tx: dict[str, str]) -> None:
        del tx
        if self._call_error is not None:
            raise self._call_error

    def build_transaction(self, tx: dict[str, str]) -> dict[str, str]:
        if self._gas_error is not None:
            raise self._gas_error
        return tx


class _FakeKickerFunctions:
    def __init__(self, owner: str, keepers: dict[str, bool], enable_fn: _FakeEnableFn) -> None:
        self._owner = owner
        self._keepers = keepers
        self._enable_fn = enable_fn

    def owner(self):
        owner = self._owner
        return type("_Call", (), {"call": lambda _self: owner})()

    def keeper(self, caller: str):
        keepers = self._keepers
        return type("_Call", (), {"call": lambda _self: keepers.get(caller.lower(), False)})()

    def enableTokens(self, auction: str, tokens: list[str]) -> _FakeEnableFn:
        del auction, tokens
        return self._enable_fn


class _FakeContract:
    def __init__(self, functions) -> None:  # noqa: ANN001
        self.functions = functions


class _FakeEth:
    def __init__(self, contract, gas_estimate: int) -> None:  # noqa: ANN001
        self._contract = contract
        self._gas_estimate = gas_estimate

    def contract(self, *, address: str, abi):  # noqa: ANN001
        del address, abi
        return self._contract

    def estimate_gas(self, tx: dict[str, str]) -> int:
        del tx
        return self._gas_estimate


class _FakeWeb3:
    def __init__(self, contract, gas_estimate: int = 210_000) -> None:  # noqa: ANN001
        self.eth = _FakeEth(contract, gas_estimate)


def test_build_execution_plan_uses_auction_kicker_and_keeper_auth() -> None:
    enable_fn = _FakeEnableFn()
    contract = _FakeContract(
        _FakeKickerFunctions(
            owner="0x1111111111111111111111111111111111111111",
            keepers={"0x2222222222222222222222222222222222222222": True},
            enable_fn=enable_fn,
        )
    )
    enabler = AuctionTokenEnabler(
        _FakeWeb3(contract),
        type("Settings", (), {"auction_kicker_address": "0x3333333333333333333333333333333333333333"})(),
    )

    plan = enabler.build_execution_plan(
        inspection=AuctionInspection(
            auction_address="0x4444444444444444444444444444444444444444",
            governance="0xb634316e06cc0b358437cbadd4dc94f1d3a92b3b",
            want="0x5555555555555555555555555555555555555555",
            receiver="0x6666666666666666666666666666666666666666",
            version="1.0.0",
            in_configured_factory=True,
            governance_matches_required=True,
            enabled_tokens=(),
        ),
        tokens=["0x7777777777777777777777777777777777777777"],
        caller_address="0x2222222222222222222222222222222222222222",
    )

    assert plan.to_address == "0x3333333333333333333333333333333333333333"
    assert plan.data == "0xdeadbeef"
    assert plan.call_succeeded is True
    assert plan.gas_estimate == 210_000
    assert plan.sender_authorized is True
    assert plan.authorization_target == "0x3333333333333333333333333333333333333333"


def test_build_execution_plan_rejects_governance_mismatch() -> None:
    enabler = AuctionTokenEnabler(
        _FakeWeb3(_FakeContract(_FakeKickerFunctions(owner="0x1", keepers={}, enable_fn=_FakeEnableFn()))),
        type("Settings", (), {"auction_kicker_address": "0x3333333333333333333333333333333333333333"})(),
    )

    with pytest.raises(RuntimeError, match="standard Yearn auctions via AuctionKicker"):
        enabler.build_execution_plan(
            inspection=AuctionInspection(
                auction_address="0x4444444444444444444444444444444444444444",
                governance="0x9999999999999999999999999999999999999999",
                want="0x5555555555555555555555555555555555555555",
                receiver="0x6666666666666666666666666666666666666666",
                version="1.0.0",
                in_configured_factory=True,
                governance_matches_required=False,
                enabled_tokens=(),
            ),
            tokens=["0x7777777777777777777777777777777777777777"],
            caller_address="0x2222222222222222222222222222222222222222",
        )


def test_parse_manual_token_input_normalizes_addresses() -> None:
    parsed = parse_manual_token_input(
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48, 0xD533a949740bb3306d119CC777fa900bA034cd52"
    )

    assert parsed == [
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "0xd533a949740bb3306d119cc777fa900ba034cd52",
    ]


def test_resolve_source_type_returns_fee_burner_with_warning() -> None:
    result = resolve_source_type(
        receiver="0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8",
        auction_want="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        monitored_fee_burners=[
            MonitoredFeeBurner(
                address="0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8",
                want_address="0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
                label="yCRV Fee Burner",
            )
        ],
        strategy_want=None,
    )

    assert result.source_type == "fee_burner"
    assert result.source_name == "yCRV Fee Burner"
    assert len(result.warnings) == 1


def test_resolve_source_type_returns_strategy_when_want_matches() -> None:
    result = resolve_source_type(
        receiver="0x9AD3047D578e79187f0FaEEf26729097a4973325",
        auction_want="0xf939e0a03fb07f59a73314e73794be0e57ac1b4e",
        monitored_fee_burners=[],
        strategy_want="0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
        strategy_name="Curve Strategy",
    )

    assert result.source_type == "strategy"
    assert result.source_address == "0x9ad3047d578e79187f0faeef26729097a4973325"
    assert result.source_name == "Curve Strategy"
    assert result.warnings == ()


def test_resolve_source_type_rejects_unknown_receiver() -> None:
    with pytest.raises(RuntimeError):
        resolve_source_type(
            receiver="0x9AD3047D578e79187f0FaEEf26729097a4973325",
            auction_want="0xf939e0a03fb07f59a73314e73794be0e57ac1b4e",
            monitored_fee_burners=[],
            strategy_want="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        )
