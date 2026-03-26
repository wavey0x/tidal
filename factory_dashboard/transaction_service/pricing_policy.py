"""Auction pricing policy loading and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from factory_dashboard.normalizers import normalize_address


_DEFAULT_POLICY_PATH = Path("auction_pricing_policy.yaml")


@dataclass(frozen=True, slots=True)
class AuctionPricingProfile:
    name: str
    start_price_buffer_bps: int
    min_price_buffer_bps: int
    step_decay_rate_bps: int


@dataclass(frozen=True, slots=True)
class AuctionPricingPolicy:
    default_profile_name: str
    profiles: dict[str, AuctionPricingProfile]
    auction_profile_overrides: dict[tuple[str, str], str]

    def resolve(self, auction_address: str, sell_token: str) -> AuctionPricingProfile:
        auction_key = normalize_address(auction_address)
        sell_token_key = normalize_address(sell_token)
        profile_name = self.auction_profile_overrides.get(
            (auction_key, sell_token_key),
            self.default_profile_name,
        )
        return self.profiles[profile_name]


def _coerce_bps(value: object, *, field_name: str, profile_name: str) -> int:
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{profile_name}.{field_name} must be an integer") from exc
    if output < 0:
        raise ValueError(f"{profile_name}.{field_name} must be non-negative")
    return output


def _load_raw_policy(policy_path: Path) -> dict[str, object]:
    with policy_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Policy file must contain a mapping object: {policy_path}")
    return raw


def load_auction_pricing_policy(policy_path: Path | None = None) -> AuctionPricingPolicy:
    resolved_path = policy_path or (Path.cwd() / _DEFAULT_POLICY_PATH)
    raw = _load_raw_policy(resolved_path)

    default_profile_name = str(raw.get("default_profile") or "").strip()
    if not default_profile_name:
        raise ValueError("auction pricing policy must define default_profile")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("auction pricing policy must define profiles")

    profiles: dict[str, AuctionPricingProfile] = {}
    for profile_name, profile_raw in raw_profiles.items():
        if not isinstance(profile_raw, dict):
            raise ValueError(f"profile {profile_name} must be a mapping")
        profile_key = str(profile_name).strip()
        if not profile_key:
            raise ValueError("profile names must be non-empty")
        profiles[profile_key] = AuctionPricingProfile(
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

    raw_auctions = raw.get("auctions") or {}
    if not isinstance(raw_auctions, dict):
        raise ValueError("auctions must be a mapping")

    overrides: dict[tuple[str, str], str] = {}
    for auction_address, raw_sell_tokens in raw_auctions.items():
        if not isinstance(raw_sell_tokens, dict):
            raise ValueError(f"auction override for {auction_address} must be a mapping")
        normalized_auction = normalize_address(str(auction_address))
        for sell_token, profile_name in raw_sell_tokens.items():
            profile_key = str(profile_name).strip()
            if profile_key not in profiles:
                raise ValueError(f"profile {profile_key!r} is not defined")
            overrides[(normalized_auction, normalize_address(str(sell_token)))] = profile_key

    return AuctionPricingPolicy(
        default_profile_name=default_profile_name,
        profiles=profiles,
        auction_profile_overrides=overrides,
    )
