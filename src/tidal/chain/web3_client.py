"""Async web3 client wrapper."""

from __future__ import annotations

import asyncio
from typing import Any

from eth_utils import to_checksum_address
from hexbytes import HexBytes
from web3 import AsyncHTTPProvider, AsyncWeb3

from tidal.chain.retry import call_with_retries


class Web3Client:
    """Wrapper around AsyncWeb3 with retry and timeout controls."""

    def __init__(self, rpc_url: str, *, timeout_seconds: int, retry_attempts: int):
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts

    def contract(self, address: str, abi: list[dict[str, Any]]) -> Any:
        return self.w3.eth.contract(address=to_checksum_address(address), abi=abi)

    async def call(self, call_fn: Any, **call_kwargs: Any) -> Any:
        async def _call() -> Any:
            return await asyncio.wait_for(call_fn.call(**call_kwargs), timeout=self.timeout_seconds)

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_block_number(self) -> int:
        async def _call() -> int:
            return await asyncio.wait_for(self.w3.eth.block_number, timeout=self.timeout_seconds)

        return int(await call_with_retries(_call, attempts=self.retry_attempts))

    async def eth_call_raw(
        self,
        target: str,
        call_data: bytes,
        *,
        block_identifier: str | int = "latest",
    ) -> bytes:
        async def _call() -> bytes:
            response = await asyncio.wait_for(
                self.w3.eth.call(
                    {
                        "to": to_checksum_address(target),
                        "data": HexBytes(call_data),
                    },
                    block_identifier=block_identifier,
                ),
                timeout=self.timeout_seconds,
            )
            return bytes(response)

        return bytes(await call_with_retries(_call, attempts=self.retry_attempts))
