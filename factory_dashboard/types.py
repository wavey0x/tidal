"""Shared types used across modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class DiscoveredStrategy:
    strategy_address: str
    vault_address: str


@dataclass(slots=True)
class TokenMetadata:
    address: str
    chain_id: int
    name: str | None
    symbol: str | None
    decimals: int
    is_core_reward: bool
    first_seen_at: str
    last_seen_at: str


@dataclass(slots=True)
class TokenLogoState:
    address: str
    logo_url: str | None
    logo_status: str | None
    logo_validated_at: str | None


@dataclass(slots=True)
class ScanItemError:
    stage: str
    error_code: str
    error_message: str
    source_type: str | None = None
    source_address: str | None = None
    token_address: str | None = None


@dataclass(slots=True)
class BalanceResult:
    source_address: str
    token_address: str
    raw_balance: int
    normalized_balance: str
    block_number: int
    scanned_at: datetime


@dataclass(slots=True, frozen=True)
class BalancePair:
    source_address: str
    token_address: str


@dataclass(slots=True)
class ScanRunResult:
    run_id: str
    status: str
    vaults_seen: int
    strategies_seen: int
    pairs_seen: int
    pairs_succeeded: int
    pairs_failed: int
