"""Stable first-party token logo resource identifiers."""

from __future__ import annotations

from tidal.constants import PRICE_TOKEN_ALIAS_TO_CANONICAL
from tidal.normalizers import normalize_address

TOKEN_LOGO_ORIGIN = "https://prices.wavey.info"


def token_logo_url(*, chain_id: int, address: str) -> str:
    """Return the stable owned logo resource without probing for availability."""
    if chain_id <= 0:
        raise ValueError("chain_id must be positive")
    normalized = normalize_address(address)
    logo_address = PRICE_TOKEN_ALIAS_TO_CANONICAL.get(normalized, normalized)
    return f"{TOKEN_LOGO_ORIGIN}/token-logos/{chain_id}/{logo_address}"
