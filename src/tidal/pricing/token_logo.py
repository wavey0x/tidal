"""Token logo URL validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from tidal.chain.retry import call_with_retries


@dataclass(slots=True)
class TokenLogoValidationResult:
    logo_url: str | None
    status: str
    error_message: str | None


class TokenLogoValidator:
    """Validates that a candidate token logo URL resolves to an image."""

    source_name = "token_price_agg_logo_url"

    def __init__(self, *, timeout_seconds: int, retry_attempts: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts

    async def validate(self, logo_url: str | None) -> TokenLogoValidationResult:
        if logo_url is None or not str(logo_url).strip():
            return TokenLogoValidationResult(
                logo_url=None,
                status="NOT_FOUND",
                error_message=None,
            )

        normalized = str(logo_url).strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return TokenLogoValidationResult(
                logo_url=None,
                status="INVALID",
                error_message="invalid logo url",
            )

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout_seconds,
            headers={"accept": "image/*"},
        ) as client:
            try:
                return await call_with_retries(
                    lambda: self._validate_once(client, normalized),
                    attempts=self.retry_attempts,
                )
            except httpx.InvalidURL as exc:
                return TokenLogoValidationResult(
                    logo_url=None,
                    status="INVALID",
                    error_message=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                return TokenLogoValidationResult(
                    logo_url=None,
                    status="FAILED",
                    error_message=str(exc),
                )

    async def _validate_once(
        self,
        client: httpx.AsyncClient,
        logo_url: str,
    ) -> TokenLogoValidationResult:
        async with client.stream("GET", logo_url) as response:
            if response.status_code == 404:
                return TokenLogoValidationResult(
                    logo_url=None,
                    status="NOT_FOUND",
                    error_message="logo url returned 404",
                )
            if response.status_code != 200:
                return TokenLogoValidationResult(
                    logo_url=None,
                    status="FAILED",
                    error_message=f"logo url returned status {response.status_code}",
                )

            content_type = (response.headers.get("content-type") or "").lower()
            if not content_type.startswith("image/"):
                return TokenLogoValidationResult(
                    logo_url=None,
                    status="INVALID",
                    error_message=f"unexpected content type: {content_type or 'missing'}",
                )

            return TokenLogoValidationResult(
                logo_url=logo_url,
                status="SUCCESS",
                error_message=None,
            )
