"""Unit tests for shared auction state reads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eth_abi import encode

from tidal.scanner.auction_state import AuctionStateReader


class _FakeFunction:
    def __init__(self, name: str, args: tuple[object, ...] = ()) -> None:
        self.name = name
        self.args = args

    def _encode_transaction_data(self) -> bytes:
        return f"{self.name}:{','.join(map(str, self.args))}".encode()


class _FakeFunctions:
    def getAllEnabledAuctions(self):
        return _FakeFunction("getAllEnabledAuctions")

    def version(self):
        return _FakeFunction("version")


class _FakeContract:
    def __init__(self) -> None:
        self.functions = _FakeFunctions()


class _FakeWeb3Client:
    def __init__(self, direct_values=None) -> None:
        self.direct_values = direct_values or {}

    def contract(self, address, abi):  # noqa: ARG002
        return _FakeContract()

    async def call(self, call_fn, **call_kwargs):  # noqa: ARG002
        return self.direct_values[call_fn.name]


class _FakeMulticallClient:
    def __init__(self, values) -> None:
        self.values = values

    async def execute(self, requests, *, batch_size, allow_failure=True, block="latest"):  # noqa: ARG002
        results = []
        for request in requests:
            value = self.values.get(request.logical_key)
            if value is None:
                results.append(SimpleNamespace(logical_key=request.logical_key, success=False, return_data=b""))
                continue
            results.append(SimpleNamespace(logical_key=request.logical_key, success=True, return_data=value))
        return results


@pytest.mark.asyncio
async def test_read_address_array_noargs_many_decodes_multicall_results() -> None:
    reader = AuctionStateReader(
        web3_client=_FakeWeb3Client(),
        multicall_client=_FakeMulticallClient(
            {
                ("0x1111111111111111111111111111111111111111", "getAllEnabledAuctions"): encode(
                    ["address[]"],
                    [["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"]],
                ),
            }
        ),
        multicall_enabled=True,
        multicall_auction_batch_calls=10,
    )

    result = await reader.read_address_array_noargs_many(
        [
            "0x1111111111111111111111111111111111111111",
            "0x2222222222222222222222222222222222222222",
        ],
        "getAllEnabledAuctions",
    )

    assert result == {
        "0x1111111111111111111111111111111111111111": ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"],
        "0x2222222222222222222222222222222222222222": None,
    }


@pytest.mark.asyncio
async def test_read_address_array_noargs_many_normalizes_direct_calls() -> None:
    reader = AuctionStateReader(
        web3_client=_FakeWeb3Client(
            direct_values={
                "getAllEnabledAuctions": ["0xD533a949740bb3306d119CC777fa900bA034cd52"],
            }
        ),
        multicall_client=None,
        multicall_enabled=False,
        multicall_auction_batch_calls=10,
    )

    result = await reader.read_address_array_noargs_many(
        ["0x1111111111111111111111111111111111111111"],
        "getAllEnabledAuctions",
    )

    assert result == {
        "0x1111111111111111111111111111111111111111": ["0xd533a949740bb3306d119cc777fa900ba034cd52"],
    }


@pytest.mark.asyncio
async def test_read_string_noargs_many_uses_minimal_version_read() -> None:
    address = "0x1111111111111111111111111111111111111111"
    reader = AuctionStateReader(
        web3_client=_FakeWeb3Client(),
        multicall_client=_FakeMulticallClient(
            {(address, "version"): encode(["string"], ["1.0.5"])}
        ),
        multicall_enabled=True,
        multicall_auction_batch_calls=10,
    )

    assert await reader.read_string_noargs_many([address], "version") == {
        address: "1.0.5"
    }
