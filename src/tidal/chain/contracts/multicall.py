"""Multicall3 client and request/result models."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

import structlog
from eth_utils import to_checksum_address
from hexbytes import HexBytes

from tidal.chain.contracts.abis import MULTICALL3_ABI
from tidal.chain.retry import is_retryable_error
from tidal.normalizers import normalize_address

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class MulticallRequest:
    target: str
    call_data: bytes
    logical_key: tuple[str, ...]


@dataclass(slots=True)
class MulticallResult:
    logical_key: tuple[str, ...]
    success: bool
    return_data: bytes
    via_fallback: bool = False
    error_message: str | None = None


@dataclass(slots=True)
class MulticallExecutionStats:
    batch_count: int = 0
    subcalls_total: int = 0
    subcalls_failed: int = 0
    fallback_direct_calls_total: int = 0


class MulticallClient:
    """Executes batched eth_call operations via Multicall3."""

    def __init__(
        self,
        web3_client,
        multicall_address: str,
        *,
        enabled: bool,
    ):
        self.web3_client = web3_client
        self.multicall_address = normalize_address(multicall_address)
        self.enabled = enabled
        self._disabled_for_run = False
        self._disable_reason_logged = False
        self.last_stats = MulticallExecutionStats()

    def begin_run(self) -> None:
        self._disabled_for_run = False
        self._disable_reason_logged = False
        self.last_stats = MulticallExecutionStats()

    async def execute(
        self,
        calls: list[MulticallRequest],
        *,
        batch_size: int,
        block: str | int = "latest",
        allow_failure: bool = True,
    ) -> list[MulticallResult]:
        """Execute calls in deterministic order with chunk-level fallback."""

        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")

        stats = MulticallExecutionStats()
        if not calls:
            self.last_stats = stats
            return []

        results: list[MulticallResult] = []
        chunks = _chunk(calls, batch_size)

        for chunk in chunks:
            stats.batch_count += 1
            stats.subcalls_total += len(chunk)

            if self.enabled and not self._disabled_for_run:
                try:
                    chunk_results = await self._execute_multicall_chunk(
                        chunk,
                        block=block,
                        allow_failure=allow_failure,
                    )
                except Exception as exc:  # noqa: BLE001
                    if not is_retryable_error(exc):
                        self._disabled_for_run = True
                        if not self._disable_reason_logged:
                            logger.warning(
                                "multicall_disabled_for_run",
                                error=str(exc),
                                multicall_address=self.multicall_address,
                            )
                            self._disable_reason_logged = True

                    chunk_results = await self._execute_direct_chunk(chunk, block=block)
                    stats.fallback_direct_calls_total += len(chunk)
            else:
                chunk_results = await self._execute_direct_chunk(chunk, block=block)
                stats.fallback_direct_calls_total += len(chunk)

            stats.subcalls_failed += sum(1 for item in chunk_results if not item.success)
            results.extend(chunk_results)

        self.last_stats = stats
        return results

    async def _execute_multicall_chunk(
        self,
        chunk: list[MulticallRequest],
        *,
        block: str | int,
        allow_failure: bool,
    ) -> list[MulticallResult]:
        contract = self.web3_client.contract(self.multicall_address, MULTICALL3_ABI)
        aggregate_calls = [
            {
                "target": to_checksum_address(call.target),
                "allowFailure": allow_failure,
                "callData": HexBytes(call.call_data),
            }
            for call in chunk
        ]
        call_fn = contract.functions.aggregate3(aggregate_calls)
        raw_results = await self.web3_client.call(call_fn, block_identifier=block)

        mapped: list[MulticallResult] = []
        for request, raw in zip(chunk, raw_results, strict=True):
            if isinstance(raw, dict):
                success = bool(raw["success"])
                return_data = bytes(raw["returnData"])
            else:
                success = bool(raw[0])
                return_data = bytes(raw[1])

            mapped.append(
                MulticallResult(
                    logical_key=request.logical_key,
                    success=success,
                    return_data=return_data,
                    via_fallback=False,
                )
            )

        return mapped

    async def _execute_direct_chunk(
        self,
        chunk: list[MulticallRequest],
        *,
        block: str | int,
    ) -> list[MulticallResult]:
        mapped: list[MulticallResult] = []

        for request in chunk:
            try:
                return_data = await self.web3_client.eth_call_raw(
                    request.target,
                    request.call_data,
                    block_identifier=block,
                )
                mapped.append(
                    MulticallResult(
                        logical_key=request.logical_key,
                        success=True,
                        return_data=return_data,
                        via_fallback=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                mapped.append(
                    MulticallResult(
                        logical_key=request.logical_key,
                        success=False,
                        return_data=b"",
                        via_fallback=True,
                        error_message=str(exc),
                    )
                )

        return mapped


def _chunk(calls: list[MulticallRequest], batch_size: int) -> list[list[MulticallRequest]]:
    if not calls:
        return []

    count = ceil(len(calls) / batch_size)
    return [calls[index * batch_size : (index + 1) * batch_size] for index in range(count)]
