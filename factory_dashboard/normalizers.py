"""Normalization helpers for addresses and numeric balance values."""

from __future__ import annotations

from decimal import Decimal, getcontext

from eth_utils import is_address

from factory_dashboard.errors import AddressNormalizationError

getcontext().prec = 78


def normalize_address(address: str) -> str:
    """Validate and normalize an EVM address to lowercase representation."""

    if not isinstance(address, str):
        raise AddressNormalizationError("address must be a string")
    if not is_address(address):
        raise AddressNormalizationError(f"invalid address: {address}")
    return address.lower()


def to_decimal_string(raw_balance: int, decimals: int) -> str:
    """Convert raw integer token units into a plain decimal string."""

    if decimals < 0:
        raise ValueError("decimals must be non-negative")

    scaled = Decimal(raw_balance) / (Decimal(10) ** decimals)
    normalized = format(scaled.normalize(), "f")

    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")

    if normalized in {"", "-0"}:
        return "0"

    return normalized


def short_address(address: str) -> str:
    """Shorten an EVM address to 0x1234…5678 form."""

    if len(address) < 10:
        return address
    return f"{address[:6]}\u2026{address[-4:]}"
