"""Kick policy loading and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from pathlib import Path

import yaml

from tidal.normalizers import normalize_address


def _normalize_lookup_value(value: str) -> str:
    try:
        return normalize_address(value)
    except Exception:
        return str(value).lower()


@dataclass(frozen=True, slots=True)
class PricingProfile:
    name: str
    start_price_buffer_bps: int
    min_price_buffer_bps: int
    step_decay_rate_bps: int


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    default_profile_name: str
    profiles: dict[str, PricingProfile]
    profile_overrides: dict[tuple[str, str], str]

    def resolve(self, auction_address: str, sell_token: str) -> PricingProfile:
        auction_key = normalize_address(auction_address)
        sell_token_key = normalize_address(sell_token)
        profile_name = self.profile_overrides.get(
            (auction_key, sell_token_key),
            self.default_profile_name,
        )
        return self.profiles[profile_name]


@dataclass(frozen=True, slots=True)
class TokenSizingPolicy:
    token_overrides: dict[str, Decimal]

    def resolve(self, token_address: str) -> Decimal | None:
        return self.token_overrides.get(normalize_address(token_address))


@dataclass(frozen=True, slots=True)
class IgnorePolicy:
    ignored_sources: frozenset[str]
    ignored_auctions: frozenset[str]
    ignored_auction_tokens: frozenset[tuple[str, str]]

    def match(self, *, source_address: str, auction_address: str, token_address: str) -> str | None:
        if not self.ignored_sources and not self.ignored_auctions and not self.ignored_auction_tokens:
            return None

        normalized_source = _normalize_lookup_value(source_address)
        normalized_auction = _normalize_lookup_value(auction_address)
        normalized_token = _normalize_lookup_value(token_address)

        if (normalized_auction, normalized_token) in self.ignored_auction_tokens:
            return "auction/token"
        if normalized_auction in self.ignored_auctions:
            return "auction"
        if normalized_source in self.ignored_sources:
            return "source"
        return None


@dataclass(frozen=True, slots=True)
class CooldownPolicy:
    default_minutes: int
    auction_token_overrides_minutes: dict[tuple[str, str], int]

    def resolve_minutes(self, *, auction_address: str, token_address: str) -> int:
        normalized_auction = _normalize_lookup_value(auction_address)
        normalized_token = _normalize_lookup_value(token_address)
        return self.auction_token_overrides_minutes.get(
            (normalized_auction, normalized_token),
            self.default_minutes,
        )


@dataclass(frozen=True, slots=True)
class KickConfig:
    pricing_policy: PricingPolicy
    token_sizing_policy: TokenSizingPolicy
    ignore_policy: IgnorePolicy
    cooldown_policy: CooldownPolicy


def _coerce_bps(value: object, *, field_name: str, profile_name: str) -> int:
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{profile_name}.{field_name} must be an integer") from exc
    if output < 0:
        raise ValueError(f"{profile_name}.{field_name} must be non-negative")
    return output


def _coerce_non_negative_int(value: object, *, field_name: str) -> int:
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if output < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return output


def _coerce_positive_decimal(value: object, *, field_name: str, scope_name: str) -> Decimal:
    try:
        output = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{scope_name}.{field_name} must be a number") from exc
    if output <= 0:
        raise ValueError(f"{scope_name}.{field_name} must be greater than zero")
    return output


def _load_raw_kick_config(kick_path: Path) -> dict[str, object]:
    with kick_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Kick config file must contain a mapping object: {kick_path}")
    return raw


def _build_pricing_policy(raw: Mapping[str, object]) -> PricingPolicy:
    default_profile_name = str(raw.get("default_profile") or "").strip()
    if not default_profile_name:
        raise ValueError("kick config must define default_profile")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("kick config must define profiles")

    profiles: dict[str, PricingProfile] = {}
    for profile_name, profile_raw in raw_profiles.items():
        if not isinstance(profile_raw, dict):
            raise ValueError(f"profile {profile_name} must be a mapping")
        profile_key = str(profile_name).strip()
        if not profile_key:
            raise ValueError("profile names must be non-empty")
        profiles[profile_key] = PricingProfile(
            name=profile_key,
            start_price_buffer_bps=_coerce_bps(
                profile_raw.get("start_price_buffer_bps"),
                field_name="start_price_buffer_bps",
                profile_name=profile_key,
            ),
            min_price_buffer_bps=_coerce_bps(
                profile_raw.get("min_price_buffer_bps"),
                field_name="min_price_buffer_bps",
                profile_name=profile_key,
            ),
            step_decay_rate_bps=_coerce_bps(
                profile_raw.get("step_decay_rate_bps"),
                field_name="step_decay_rate_bps",
                profile_name=profile_key,
            ),
        )

    if default_profile_name not in profiles:
        raise ValueError(f"default profile {default_profile_name!r} is not defined")

    if "auctions" in raw:
        raise ValueError("auctions is no longer supported; use profile_overrides")

    raw_profile_overrides = raw.get("profile_overrides") or []
    if not isinstance(raw_profile_overrides, list):
        raise ValueError("profile_overrides must be a list")

    overrides: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(raw_profile_overrides):
        if not isinstance(entry, dict):
            raise ValueError(f"profile_overrides[{index}] must be a mapping")

        auction_value = entry.get("auction")
        token_value = entry.get("token")
        profile_value = entry.get("profile")
        if auction_value is None or token_value is None or profile_value is None:
            raise ValueError(f"profile_overrides[{index}] must define auction, token, and profile")

        profile_key = str(profile_value).strip()
        if not profile_key:
            raise ValueError(f"profile_overrides[{index}].profile must be non-empty")
        if profile_key not in profiles:
            raise ValueError(f"profile {profile_key!r} is not defined")

        rule = (
            normalize_address(str(auction_value)),
            normalize_address(str(token_value)),
        )
        if rule in overrides:
            raise ValueError(f"duplicate profile override: {rule[0]} {rule[1]}")
        overrides[rule] = profile_key

    return PricingPolicy(
        default_profile_name=default_profile_name,
        profiles=profiles,
        profile_overrides=overrides,
    )


def _build_token_sizing_policy(raw: Mapping[str, object]) -> TokenSizingPolicy:
    raw_limits = raw.get("usd_kick_limit") or {}
    if not isinstance(raw_limits, dict):
        raise ValueError("usd_kick_limit must be a mapping")

    token_overrides: dict[str, Decimal] = {}
    for token_address, raw_limit in raw_limits.items():
        token_overrides[normalize_address(str(token_address))] = _coerce_positive_decimal(
            raw_limit,
            field_name="value",
            scope_name=f"usd_kick_limit[{token_address}]",
        )

    return TokenSizingPolicy(token_overrides=token_overrides)


def _build_ignore_policy(raw: Mapping[str, object]) -> IgnorePolicy:
    raw_ignore = raw.get("ignore") or []
    if not isinstance(raw_ignore, list):
        raise ValueError("ignore must be a list")

    ignored_sources: set[str] = set()
    ignored_auctions: set[str] = set()
    ignored_auction_tokens: set[tuple[str, str]] = set()

    for index, entry in enumerate(raw_ignore):
        if not isinstance(entry, dict):
            raise ValueError(f"ignore[{index}] must be a mapping")

        has_source = entry.get("source") is not None
        has_auction = entry.get("auction") is not None
        if has_source == has_auction:
            raise ValueError(f"ignore[{index}] must define exactly one of source or auction")

        token_value = entry.get("token")
        if has_source:
            if token_value is not None:
                raise ValueError(f"ignore[{index}].token is only valid with auction rules")
            normalized_source = normalize_address(str(entry["source"]))
            if normalized_source in ignored_sources:
                raise ValueError(f"duplicate ignore source rule: {normalized_source}")
            ignored_sources.add(normalized_source)
            continue

        normalized_auction = normalize_address(str(entry["auction"]))
        if token_value is None:
            if normalized_auction in ignored_auctions:
                raise ValueError(f"duplicate ignore auction rule: {normalized_auction}")
            ignored_auctions.add(normalized_auction)
            continue

        rule = (normalized_auction, normalize_address(str(token_value)))
        if rule in ignored_auction_tokens:
            raise ValueError(f"duplicate ignore auction/token rule: {rule[0]} {rule[1]}")
        ignored_auction_tokens.add(rule)

    return IgnorePolicy(
        ignored_sources=frozenset(ignored_sources),
        ignored_auctions=frozenset(ignored_auctions),
        ignored_auction_tokens=frozenset(ignored_auction_tokens),
    )


def _build_cooldown_policy(raw: Mapping[str, object]) -> CooldownPolicy:
    default_minutes = _coerce_non_negative_int(raw.get("cooldown_minutes", 60), field_name="cooldown_minutes")

    raw_cooldown = raw.get("cooldown") or []
    if not isinstance(raw_cooldown, list):
        raise ValueError("cooldown must be a list")

    overrides: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(raw_cooldown):
        if not isinstance(entry, dict):
            raise ValueError(f"cooldown[{index}] must be a mapping")

        auction_value = entry.get("auction")
        token_value = entry.get("token")
        minutes_value = entry.get("minutes")
        if auction_value is None or token_value is None or minutes_value is None:
            raise ValueError(f"cooldown[{index}] must define auction, token, and minutes")

        normalized_auction = normalize_address(str(auction_value))
        normalized_token = normalize_address(str(token_value))
        rule = (normalized_auction, normalized_token)
        if rule in overrides:
            raise ValueError(f"duplicate cooldown rule: {normalized_auction} {normalized_token}")
        overrides[rule] = _coerce_non_negative_int(minutes_value, field_name=f"cooldown[{index}].minutes")

    return CooldownPolicy(
        default_minutes=default_minutes,
        auction_token_overrides_minutes=overrides,
    )


def build_kick_config(raw: Mapping[str, object] | None = None) -> KickConfig:
    resolved_raw = raw or {}
    return KickConfig(
        pricing_policy=_build_pricing_policy(resolved_raw),
        token_sizing_policy=_build_token_sizing_policy(resolved_raw),
        ignore_policy=_build_ignore_policy(resolved_raw),
        cooldown_policy=_build_cooldown_policy(resolved_raw),
    )


def load_kick_config(kick_path: Path | None = None) -> KickConfig:
    if kick_path is None:
        raise ValueError("kick_path is required")
    resolved_path = Path(kick_path).expanduser().resolve()
    return build_kick_config(_load_raw_kick_config(resolved_path))
