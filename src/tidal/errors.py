"""Custom exceptions for scanner runtime."""

from __future__ import annotations


class TidalError(Exception):
    """Base error for all scanner-specific failures."""


class ConfigurationError(TidalError):
    """Raised when required runtime configuration is missing or invalid."""


class AddressNormalizationError(TidalError):
    """Raised when an address cannot be normalized."""
