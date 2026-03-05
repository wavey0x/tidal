"""Token price refresh orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from tidal.constants import PRICE_TOKEN_ALIAS_TO_CANONICAL
from tidal.pricing.token_price_agg import TokenPriceNotFoundError
from tidal.normalizers import normalize_address
from tidal.time import utcnow_iso
from tidal.types import ScanItemError


@dataclass(slots=True, frozen=True)
class PriceToken:
    address: str
    decimals: int


class TokenPriceRefreshService:
    """Refreshes latest token prices once per unique token per scan."""

    def __init__(
        self,
        *,
        chain_id: int,
        enabled: bool,
        concurrency: int,
        price_provider,
        token_repository,
    ):
        self.chain_id = chain_id
        self.enabled = enabled
        self.concurrency = max(1, concurrency)
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

        async def _refresh_token(token: PriceToken) -> tuple[str, str, str | None, str | None]:
            async with sem:
                try:
                    quote = await self.price_provider.quote_usd(token.address, token.decimals)
                except TokenPriceNotFoundError as exc:
                    return token.address, "NOT_FOUND", None, str(exc)
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        return token.address, "NOT_FOUND", None, str(exc)
                except Exception as exc:  # noqa: BLE001
                    return token.address, "FAILED", None, str(exc)

                return token.address, "SUCCESS", str(quote.price_usd), None

        results = await asyncio.gather(*[_refresh_token(token) for token in unique_tokens])

        for canonical_address, status, price_usd, error_message in results:
            original_addresses = canonical_groups.get(canonical_address, [canonical_address])
            fetched_at = utcnow_iso()

            for original_address in original_addresses:
                self.token_repository.set_latest_price(
                    address=original_address,
                    price_usd=price_usd,
                    source=self.price_provider.source_name,
                    status=status,
                    fetched_at=fetched_at,
                    run_id=run_id,
                    error_message=error_message,
                )

            if status == "SUCCESS":
                stats["tokens_succeeded"] += len(original_addresses)
                continue
            if status == "NOT_FOUND":
                stats["tokens_not_found"] += len(original_addresses)
                continue

            stats["tokens_failed"] += len(original_addresses)
            for original_address in original_addresses:
                errors.append(
                    ScanItemError(
                        stage="PRICE_READ",
                        error_code="token_price_lookup_failed",
                        error_message=error_message or "token price lookup failed",
                        token_address=original_address,
                    )
                )

        return stats, errors
