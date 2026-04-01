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

    async def close(self) -> None:
        await self.w3.provider.disconnect()

    async def call(self, call_fn: Any, **call_kwargs: Any) -> Any:
        async def _call() -> Any:
            return await asyncio.wait_for(call_fn.call(**call_kwargs), timeout=self.timeout_seconds)

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_block_number(self) -> int:
        async def _call() -> int:
            return await asyncio.wait_for(self.w3.eth.block_number, timeout=self.timeout_seconds)

        return int(await call_with_retries(_call, attempts=self.retry_attempts))

    async def get_latest_block_timestamp(self) -> int:
        async def _call() -> int:
            block = await asyncio.wait_for(
                self.w3.eth.get_block("latest"),
                timeout=self.timeout_seconds,
            )
            return int(block["timestamp"])

        return int(await call_with_retries(_call, attempts=self.retry_attempts))

    async def get_balance(self, address: str) -> int:
        async def _call() -> int:
            balance = await asyncio.wait_for(
                self.w3.eth.get_balance(to_checksum_address(address)),
                timeout=self.timeout_seconds,
            )
            return int(balance)

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_gas_price(self) -> int:
        async def _call() -> int:
            return int(await asyncio.wait_for(self.w3.eth.gas_price, timeout=self.timeout_seconds))

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_base_fee(self) -> int:
        async def _call() -> int:
            block = await asyncio.wait_for(
                self.w3.eth.get_block("latest"),
                timeout=self.timeout_seconds,
            )
            return int(block["baseFeePerGas"])

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_max_priority_fee(self) -> int:
        async def _call() -> int:
            return int(await asyncio.wait_for(self.w3.eth.max_priority_fee, timeout=self.timeout_seconds))

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_transaction_count(self, address: str) -> int:
        async def _call() -> int:
            count = await asyncio.wait_for(
                self.w3.eth.get_transaction_count(to_checksum_address(address), "pending"),
                timeout=self.timeout_seconds,
            )
            return int(count)

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        async def _call() -> int:
            estimate = await asyncio.wait_for(
                self.w3.eth.estimate_gas(tx),
                timeout=self.timeout_seconds,
            )
            return int(estimate)

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def send_raw_transaction(self, signed_tx: bytes) -> str:
        async def _call() -> str:
            tx_hash = await asyncio.wait_for(
                self.w3.eth.send_raw_transaction(signed_tx),
                timeout=self.timeout_seconds,
            )
            return "0x" + tx_hash.hex()

        return await call_with_retries(_call, attempts=self.retry_attempts)

    async def get_transaction_receipt(self, tx_hash: str, *, timeout_seconds: int = 120) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            try:
                receipt = await asyncio.wait_for(
                    self.w3.eth.get_transaction_receipt(HexBytes(tx_hash)),
                    timeout=self.timeout_seconds,
                )
                return dict(receipt)
            except Exception:  # noqa: BLE001
                if asyncio.get_event_loop().time() >= deadline:
                    raise
                await asyncio.sleep(2)

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
