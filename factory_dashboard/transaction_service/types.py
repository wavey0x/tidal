"""Types for the transaction service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


SourceType = Literal["strategy", "fee_burner"]
OperationType = Literal["kick", "settle", "sweep_and_settle"]


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
    """Row from the shortlist query — a (source, token) pair above threshold."""

    source_type: str
    source_address: str
    token_address: str
    auction_address: str
    normalized_balance: str
    price_usd: str
    want_address: str
    usd_value: float
    decimals: int
    source_name: str | None = None
    context_type: str | None = None
    context_address: str | None = None
    context_name: str | None = None
    context_symbol: str | None = None
    token_symbol: str | None = None
    want_symbol: str | None = None

    @property
    def strategy_address(self) -> str:
        return self.source_address

    @property
    def strategy_name(self) -> str | None:
        return self.source_name


@dataclass(slots=True)
class PreparedKick:
    """Output of the prepare phase — everything needed to include this kick in a batch."""

    candidate: KickCandidate
    sell_amount: int
    starting_price_raw: int
    minimum_price_raw: int
    sell_amount_str: str
    starting_price_str: str
    minimum_price_str: str
    usd_value_str: str
    live_balance_raw: int
    normalized_balance: str
    quote_amount_str: str
    start_price_buffer_bps: int
    min_price_buffer_bps: int
    step_decay_rate_bps: int
    pricing_profile_name: str
    settle_token: str | None = None
    quote_response_json: str | None = None


@dataclass(slots=True)
class KickDecision:
    """Evaluator output — whether to kick and why."""

    candidate: KickCandidate
    action: KickAction
    skip_reason: SkipReason | None = None


@dataclass(slots=True)
class PreparedSweepAndSettle:
    """Prepared stuck-auction abort operation."""

    candidate: KickCandidate
    sell_token: str
    minimum_price_raw: int | None
    available_raw: int | None
    sell_amount_str: str | None
    minimum_price_str: str | None
    usd_value_str: str | None
    normalized_balance: str | None
    stuck_abort_reason: str
    token_symbol: str | None = None


@dataclass(slots=True)
class AuctionInspection:
    """Live auction status snapshot for a candidate auction."""

    auction_address: str
    is_active_auction: bool | None
    active_tokens: tuple[str, ...]
    active_token: str | None = None
    active_available_raw: int | None = None
    active_price_raw: int | None = None
    minimum_price_raw: int | None = None


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
    minimum_price: str | None = None
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
    failure_summary: dict[str, int] | None = None
