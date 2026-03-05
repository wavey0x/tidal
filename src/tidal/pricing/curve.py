"""Curve API token pricing provider."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from tidal.chain.retry import call_with_retries
from tidal.normalizers import normalize_address


@dataclass(slots=True)
class CurveQuote:
    price_usd: Decimal
    quote_amount_in_raw: int


class CurvePriceNotFoundError(Exception):
    """Raised when Curve explicitly indicates no price is available."""


class CurvePriceProvider:
    """Fetches token/USD quotes from Curve prices API."""

    source_name = "curve_usd_price"

    def __init__(
        self,
        *,
        chain_id: int,
        base_url: str,
        timeout_seconds: int,
        retry_attempts: int,
    ):
        self.chain_id = chain_id
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.quote_token_address = "usd"
        self.quote_token_decimals = 0

    async def quote_usd(self, token_address: str, token_decimals: int) -> CurveQuote:
        normalized_token = normalize_address(token_address)
        del token_decimals

        chain_slug = _chain_slug(self.chain_id)
        path = f"/v1/usd_price/{chain_slug}/{normalized_token}"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            payload = await call_with_retries(
                lambda: self._get_price(client, path),
                attempts=self.retry_attempts,
            )

        price_usd = self._extract_price_usd(payload)
        return CurveQuote(price_usd=price_usd, quote_amount_in_raw=1)

    async def _get_price(self, client: httpx.AsyncClient, path: str) -> Any:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()

    def _extract_price_usd(self, payload: Any) -> Decimal:
        if _looks_like_not_found_payload(payload):
            raise CurvePriceNotFoundError("curve price not found in payload")

        for value in _walk_price_values(payload):
            if value < 0:
                raise ValueError("negative usd quote")
            return value

        candidate_dicts: list[dict[str, Any]] = []

        if isinstance(payload, dict):
            candidate_dicts.append(payload)
            for key in ("routes", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidate_dicts.extend([item for item in value if isinstance(item, dict)])
            route = payload.get("route")
            if isinstance(route, dict):
                candidate_dicts.append(route)
        elif isinstance(payload, list):
            candidate_dicts.extend([item for item in payload if isinstance(item, dict)])
        else:
            raise ValueError("unexpected Curve response shape")

        for key in ("price", "usd_price", "usdPrice", "value"):
            for candidate in candidate_dicts:
                value = _extract_decimal(candidate, key)
                if value is None:
                    continue
                if value < 0:
                    raise ValueError("negative usd quote")
                return value

        raise ValueError("could not parse Curve quote amount from response")


def _extract_decimal(source: dict[str, Any] | None, key: str) -> Decimal | None:
    if source is None or key not in source:
        return None

    value = source[key]
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _looks_like_not_found_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).lower()
        if "not found" in text or "no price" in text:
            return True
    return False


def _walk_price_values(payload: Any) -> list[Decimal]:
    results: list[Decimal] = []
    stack: list[tuple[Any, int]] = [(payload, 0)]
    max_depth = 8
    matched_keys = {"price", "usd_price", "price_usd", "usdprice", "usd"}

    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue

        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).lower().replace("-", "_")
                if normalized_key in matched_keys:
                    if isinstance(value, (dict, list)):
                        stack.append((value, depth + 1))
                    else:
                        parsed = _to_decimal(value)
                        if parsed is not None:
                            results.append(parsed)

                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
            continue

        if isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
            continue

    return results


def _to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _chain_slug(chain_id: int) -> str:
    if chain_id == 1:
        return "ethereum"
    raise ValueError(f"unsupported chain_id for Curve price API: {chain_id}")
