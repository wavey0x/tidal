"""Token price aggregate API provider."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from tidal.chain.retry import is_retryable_error
from tidal.normalizers import normalize_address

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class TokenPriceQuote:
    price_usd: Decimal | None
    quote_amount_in_raw: int
    logo_url: str | None = None


@dataclass(slots=True)
class QuoteResult:
    amount_out_raw: int | None
    token_out_decimals: int | None
    provider_statuses: dict[str, str | None] = field(default_factory=dict)
    raw_response: dict | None = None
    provider_amounts: dict[str, int] = field(default_factory=dict)
    request_url: str | None = None

    def curve_quote_available(self) -> bool:
        """True if the Curve provider returned a positive amount."""
        return self.provider_amounts.get("curve", 0) > 0


class TokenPriceNotFoundError(Exception):
    """Raised when the price API indicates no price is available."""


class TokenPriceAggProvider:
    """Fetches token/USD quotes from token_price_agg API."""

    source_name = "token_price_agg_usd_price"
    quote_timeout_ms = 7000
    _rate_limit_base_delay_seconds = 1.0
    _rate_limit_max_delay_seconds = 10.0
    _retry_base_delay_seconds = 0.25

    def __init__(
        self,
        *,
        chain_id: int,
        base_url: str,
        api_key: str | None,
        timeout_seconds: int,
        retry_attempts: int,
    ):
        self.chain_id = chain_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.quote_token_address = "usd"
        self.quote_token_decimals = 0
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http_client: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        """Return a shared HTTP client, creating on first use."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                headers=self._headers,
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def quote(self, token_in: str, token_out: str, amount_in: str) -> QuoteResult:
        """Fetch a direct token_in -> token_out quote via /v1/quote.

        If the first attempt returns HTTP 200 but all providers failed
        (amount_out_raw is None with providers present), retries once
        after a 2-second delay to handle transient rate-limiting.
        """
        params = {
            "token_in": normalize_address(token_in),
            "token_out": normalize_address(token_out),
            "amount_in": amount_in,
            "chain_id": self.chain_id,
            "use_underlying": "true",
            "timeout_ms": self.quote_timeout_ms,
        }
        client = await self._client()
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        request_url = f"{self.base_url}/v1/quote?{query_string}"

        max_soft_retries = 2
        for attempt in range(max_soft_retries):
            payload = await self._get_with_retries(client, "/v1/quote", params)
            result = self._parse_quote_response(payload, request_url)

            if result.amount_out_raw is not None or attempt + 1 >= max_soft_retries:
                return result

            # All providers failed — likely transient rate-limiting, retry.
            if not result.provider_statuses:
                return result

            logger.info(
                "quote_soft_retry",
                token_in=params["token_in"],
                token_out=params["token_out"],
                provider_statuses=result.provider_statuses,
                attempt=attempt + 1,
            )
            await asyncio.sleep(2.0)

        return result  # type: ignore[possibly-undefined]

    def _parse_quote_response(self, payload: Any, request_url: str) -> QuoteResult:
        """Parse a /v1/quote response payload into a QuoteResult."""
        amount_out_raw = None
        token_out_decimals = None

        if isinstance(payload, dict):
            summary = payload.get("summary")
            if isinstance(summary, dict):
                high_amount_out = summary.get("high_amount_out")
                if high_amount_out is not None:
                    parsed = _to_decimal(high_amount_out)
                    if parsed is not None:
                        amount_out_raw = int(parsed)

            token_out_data = payload.get("token_out")
            if isinstance(token_out_data, dict):
                raw_decimals = token_out_data.get("decimals")
                if raw_decimals is not None:
                    try:
                        token_out_decimals = int(raw_decimals)
                    except (ValueError, TypeError):
                        pass

        provider_statuses = {}
        provider_amounts: dict[str, int] = {}
        if isinstance(payload, dict):
            providers = payload.get("providers")
            if isinstance(providers, dict):
                for name, entry in providers.items():
                    if isinstance(entry, dict):
                        provider_statuses[name] = entry.get("status")
                        raw_amount = entry.get("amount_out")
                        if raw_amount is not None:
                            try:
                                provider_amounts[name] = int(raw_amount)
                            except (ValueError, TypeError):
                                pass

        return QuoteResult(
            amount_out_raw=amount_out_raw,
            token_out_decimals=token_out_decimals,
            provider_statuses=provider_statuses,
            raw_response=payload if isinstance(payload, dict) else None,
            provider_amounts=provider_amounts,
            request_url=request_url,
        )

    async def quote_usd(self, token_address: str, token_decimals: int) -> TokenPriceQuote:
        normalized_token = normalize_address(token_address)
        del token_decimals

        path = "/v1/price"
        params = {
            "token": normalized_token,
            "chain_id": self.chain_id,
            "use_underlying": "true",
        }
        client = await self._client()
        payload = await self._get_with_retries(client, path, params)

        logo_url = self._extract_logo_url(payload)
        try:
            price_usd = self._extract_price_usd(payload)
        except TokenPriceNotFoundError:
            price_usd = None

        return TokenPriceQuote(price_usd=price_usd, quote_amount_in_raw=1, logo_url=logo_url)

    async def _get_with_retries(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str | int],
    ) -> Any:
        attempts = max(1, self.retry_attempts)
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self._get_price(client, path, params)
            except BaseException as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= attempts or not _is_retryable_price_error(exc):
                    raise

                delay_seconds = self._retry_delay_seconds(exc, attempt)
                status_code = _http_status_code(exc)
                logger.info(
                    "price_api_retry",
                    path=path,
                    status_code=status_code,
                    attempt=attempt,
                    delay_seconds=round(delay_seconds, 3),
                )
                await asyncio.sleep(delay_seconds)

        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable price retry state")

    def _retry_delay_seconds(self, exc: BaseException, attempt: int) -> float:
        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return retry_after

        if _http_status_code(exc) == 429:
            base_delay = self._rate_limit_base_delay_seconds
            max_delay = self._rate_limit_max_delay_seconds
        else:
            base_delay = self._retry_base_delay_seconds
            max_delay = 2.0

        backoff = min(base_delay * (2 ** (attempt - 1)), max_delay)
        jitter = random.uniform(0, base_delay)
        return min(backoff + jitter, max_delay)

    async def _get_price(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str | int],
    ) -> Any:
        response = await client.get(path, params=params)
        if response.status_code == 404:
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if isinstance(payload, dict):
                return {
                    "_fd_http_status": 404,
                    **payload,
                }
            return {"_fd_http_status": 404}
        response.raise_for_status()
        return response.json()

    def _extract_price_usd(self, payload: Any) -> Decimal:
        if not isinstance(payload, dict):
            raise ValueError("unexpected price response shape")

        summary = payload.get("summary")
        if not isinstance(summary, dict):
            if _looks_like_not_found_payload(payload):
                raise TokenPriceNotFoundError("token price not found in response")
            raise ValueError("missing summary in price response")

        median_price = summary.get("median_price")
        if median_price is None:
            if _looks_like_not_found_payload(payload):
                raise TokenPriceNotFoundError("token price not found in summary.median_price")
            raise ValueError("missing summary.median_price in price response")

        price_usd = _to_decimal(median_price)
        if price_usd is None:
            raise ValueError("invalid summary.median_price in price response")
        if price_usd < 0:
            raise ValueError("negative usd quote")
        return price_usd

    def _extract_logo_url(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None

        token = payload.get("token")
        if not isinstance(token, dict):
            return None

        logo_url = token.get("logo_url")
        if logo_url is None:
            return None

        normalized = str(logo_url).strip()
        return normalized or None


def _is_retryable_price_error(exc: BaseException) -> bool:
    status_code = _http_status_code(exc)
    if status_code is not None:
        return status_code == 429 or status_code >= 500
    return is_retryable_error(exc)


def _http_status_code(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response is None:
        return None

    raw_value = exc.response.headers.get("Retry-After")
    if raw_value is None:
        return None

    stripped = raw_value.strip()
    if not stripped:
        return None

    try:
        seconds = float(stripped)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()

    return min(max(0.0, seconds), TokenPriceAggProvider._rate_limit_max_delay_seconds)


def _looks_like_not_found_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    if payload.get("_fd_http_status") == 404:
        return True

    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).lower()
        if (
            "not found" in text
            or "no price" in text
            or "unsupported token" in text
            or "unknown token" in text
        ):
            return True

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return False

    successful_providers = summary.get("successful_providers")
    median_price = summary.get("median_price")
    if successful_providers != 0 or median_price is not None:
        return False

    statuses = _collect_provider_statuses(payload.get("providers"))
    if not statuses:
        return True

    has_not_found_signal = any(status in {"no_route", "bad_request"} for status in statuses)
    has_other_signal = any(
        status not in {"no_route", "bad_request", "error"}
        for status in statuses
    )
    return has_not_found_signal and not has_other_signal


def _collect_provider_statuses(providers: Any) -> list[str]:
    if not isinstance(providers, dict):
        return []

    statuses: list[str] = []
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        value = entry.get("status")
        if value is None:
            continue
        statuses.append(str(value).lower())
    return statuses


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
