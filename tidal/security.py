"""Redaction helpers for API-facing data."""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<key>[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE))\b(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;&]+)"
)
_GENERIC_SECRET_KV_RE = re.compile(
    r"(?i)\b(?P<key>(?:api_?key|access_?token|secret|password|passphrase))\b"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;&]+)"
)
_TRAILING_URL_PUNCTUATION = ".,);]>}"
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "key",
    "password",
    "passphrase",
    "secret",
    "signature",
    "sig",
}
_REDACTED = "REDACTED"


def redact_sensitive_text(value: str | None) -> str | None:
    if value is None:
        return None

    redacted = _URL_RE.sub(_redact_url_match, value)
    redacted = _BEARER_RE.sub(f"Bearer {_REDACTED}", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(_redact_key_value_match, redacted)
    redacted = _GENERIC_SECRET_KV_RE.sub(_redact_key_value_match, redacted)
    return redacted


def redact_sensitive_data(value: Any) -> Any:
    if is_dataclass(value):
        return redact_sensitive_data(asdict(value))
    if isinstance(value, dict):
        return {key: redact_sensitive_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _redact_key_value_match(match: re.Match[str]) -> str:
    return f"{match.group('key')}{match.group('sep')}{_REDACTED}"


def _redact_url_match(match: re.Match[str]) -> str:
    original = match.group(0)
    candidate = original
    suffix = ""
    while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
        suffix = candidate[-1] + suffix
        candidate = candidate[:-1]

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return original

    if parsed.scheme.lower() not in {"http", "https"}:
        return original

    hostname = parsed.hostname or ""
    if not hostname:
        return original

    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        netloc = f"{_REDACTED}:{_REDACTED}@{netloc}"

    query_items = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.lower().replace("-", "_")
        query_items.append((key, _REDACTED if normalized_key in _SENSITIVE_QUERY_KEYS else item))

    query = urlencode(query_items, doseq=True)
    rebuilt = urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    return rebuilt + suffix
