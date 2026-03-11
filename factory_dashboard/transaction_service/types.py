"""Types for the transaction service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class KickAction(str, Enum):
    KICK = "KICK"
    SKIP = "SKIP"


class SkipReason(str, Enum):
    COOLDOWN = "COOLDOWN"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class KickStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    REVERTED = "REVERTED"
    SUBMITTED = "SUBMITTED"
    ESTIMATE_FAILED = "ESTIMATE_FAILED"
    ERROR = "ERROR"
    DRY_RUN = "DRY_RUN"
    USER_SKIPPED = "USER_SKIPPED"
    SKIP = "SKIP"


@dataclass(slots=True)
class KickCandidate:
    """Row from the shortlist query — a (strategy, token) pair above threshold."""

    strategy_address: str
    token_address: str
    auction_address: str
    normalized_balance: str
    price_usd: str
    want_address: str
    usd_value: float
    decimals: int
    strategy_name: str | None = None
    token_symbol: str | None = None
    want_symbol: str | None = None


@dataclass(slots=True)
class KickDecision:
    """Evaluator output — whether to kick and why."""

    candidate: KickCandidate
    action: KickAction
    skip_reason: SkipReason | None = None


@dataclass(slots=True)
class KickResult:
    """Kicker output — what happened when we tried to kick."""

    kick_tx_id: int
    status: KickStatus
    tx_hash: str | None = None
    gas_used: int | None = None
    gas_price_gwei: str | None = None
    block_number: int | None = None
    error_message: str | None = None
    sell_amount: str | None = None
    starting_price: str | None = None
    live_balance_raw: int | None = None
    usd_value: str | None = None


@dataclass(slots=True)
class TxnRunResult:
    """Summary of a single evaluation cycle."""

    run_id: str
    status: str
    candidates_found: int
    kicks_attempted: int
    kicks_succeeded: int
    kicks_failed: int
