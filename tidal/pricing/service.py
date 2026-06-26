"""Token price refresh orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from tidal.constants import PRICE_TOKEN_ALIAS_TO_CANONICAL
from tidal.normalizers import normalize_address
from tidal.time import utcnow_iso
from tidal.types import ScanItemError


@dataclass(slots=True, frozen=True)
class PriceToken:
    address: str
    decimals: int


@dataclass(slots=True, frozen=True)
class _PriceRefreshResult:
    address: str
    status: str
    price_usd: str | None
    error_message: str | None
    logo_url: str | None
    preserve_price_usd: bool = False


class TokenPriceRefreshService:
    """Refreshes latest token prices once per unique token per scan."""

    def __init__(
        self,
        *,
        chain_id: int,
        enabled: bool,
        concurrency: int,
        delay_seconds: float = 0,
        price_provider,
        token_repository,
    ):
        self.chain_id = chain_id
        self.enabled = enabled
        self.concurrency = max(1, concurrency)
        self.delay_seconds = delay_seconds
        self.price_provider = price_provider
        self.token_repository = token_repository

    async def refresh_many(self, *, run_id: str, tokens: list[PriceToken]) -> tuple[dict[str, int], list[ScanItemError]]:
        stats = {
            "tokens_seen": 0,
            "tokens_succeeded": 0,
            "tokens_not_found": 0,
            "tokens_failed": 0,
        }
        errors: list[ScanItemError] = []

        if not self.enabled or not tokens:
            return stats, errors

        deduped: dict[str, int] = {}
        for token in tokens:
            deduped[normalize_address(token.address)] = token.decimals

        stats["tokens_seen"] = len(deduped)

        canonical_groups: dict[str, list[str]] = {}
        canonical_decimals: dict[str, int] = {}
        for address, decimals in sorted(deduped.items()):
            canonical_address = PRICE_TOKEN_ALIAS_TO_CANONICAL.get(address, address)
            canonical_groups.setdefault(canonical_address, []).append(address)
            if canonical_address not in canonical_decimals or address == canonical_address:
                canonical_decimals[canonical_address] = decimals

        unique_tokens = [
            PriceToken(address=address, decimals=canonical_decimals[address])
            for address in sorted(canonical_groups.keys())
        ]

        sem = asyncio.Semaphore(self.concurrency)
        pace_lock = asyncio.Lock()
        next_start_at = 0.0

        async def _wait_for_start_slot() -> None:
            nonlocal next_start_at
            if self.delay_seconds <= 0:
                return

            loop = asyncio.get_running_loop()
            async with pace_lock:
                now = loop.time()
                wait_seconds = max(0.0, next_start_at - now)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    now = loop.time()
                next_start_at = now + self.delay_seconds

        async def _refresh_token(token: PriceToken) -> _PriceRefreshResult:
            async with sem:
                await _wait_for_start_slot()
                try:
                    quote = await self.price_provider.quote_usd(token.address, token.decimals)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        return _PriceRefreshResult(token.address, "NOT_FOUND", None, str(exc), None)
                    return _PriceRefreshResult(
                        token.address,
                        "FAILED",
                        None,
                        str(exc),
                        None,
                        preserve_price_usd=_is_transient_price_error(exc),
                    )
                except Exception as exc:  # noqa: BLE001
                    return _PriceRefreshResult(
                        token.address,
                        "FAILED",
                        None,
                        str(exc),
                        None,
                        preserve_price_usd=_is_transient_price_error(exc),
                    )

                status = "SUCCESS" if quote.price_usd is not None else "NOT_FOUND"
                price_usd = str(quote.price_usd) if quote.price_usd is not None else None
                return _PriceRefreshResult(token.address, status, price_usd, None, quote.logo_url)

        results = await asyncio.gather(*[_refresh_token(token) for token in unique_tokens])

        for result in results:
            canonical_address = result.address
            original_addresses = canonical_groups.get(canonical_address, [canonical_address])
            fetched_at = utcnow_iso()

            for original_address in original_addresses:
                if result.preserve_price_usd:
                    self.token_repository.mark_price_refresh_failed(
                        address=original_address,
                        source=self.price_provider.source_name,
                        status=result.status,
                        fetched_at=fetched_at,
                        run_id=run_id,
                        error_message=result.error_message,
                    )
                else:
                    self.token_repository.set_latest_price(
                        address=original_address,
                        price_usd=result.price_usd,
                        source=self.price_provider.source_name,
                        status=result.status,
                        fetched_at=fetched_at,
                        run_id=run_id,
                        error_message=result.error_message,
                    )
                if result.error_message is None:
                    self.token_repository.set_logo_url(
                        address=original_address,
                        logo_url=result.logo_url,
                    )

            if result.status == "SUCCESS":
                stats["tokens_succeeded"] += len(original_addresses)
                continue
            if result.status == "NOT_FOUND":
                stats["tokens_not_found"] += len(original_addresses)
                continue

            stats["tokens_failed"] += len(original_addresses)
            for original_address in original_addresses:
                errors.append(
                    ScanItemError(
                        stage="PRICE_READ",
                        error_code="token_price_lookup_failed",
                        error_message=result.error_message or "token price lookup failed",
                        token_address=original_address,
                    )
                )

        return stats, errors


def _is_transient_price_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response is None:
            return False
        status_code = exc.response.status_code
        return status_code == 429 or status_code >= 500

    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError, OSError))
