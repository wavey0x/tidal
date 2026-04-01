"""Types for the transaction service."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Literal

_WAD = Decimal(10) ** 18


SourceType = Literal["strategy", "fee_burner"]
OperationType = Literal["kick", "settle", "sweep_and_settle"]


class KickAction(str, Enum):
    KICK = "KICK"
    SKIP = "SKIP"


class SkipReason(str, Enum):
    IGNORED = "IGNORED"
    COOLDOWN = "COOLDOWN"


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
class KickRecoveryPlan:
    """Staged settle instructions for the extended kick path."""

    settle_after_start: tuple[str, ...] = ()
    settle_after_min: tuple[str, ...] = ()
    settle_after_decay: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.settle_after_start or self.settle_after_min or self.settle_after_decay)


@dataclass(slots=True)
class PreparedKick:
    """Output of the prepare phase — everything needed to include this kick in a batch."""

    candidate: KickCandidate
    sell_amount: int
    starting_price_unscaled: int
    minimum_price_scaled_1e18: int
    minimum_quote_unscaled: int
    sell_amount_str: str
    starting_price_unscaled_str: str
    minimum_price_scaled_1e18_str: str
    minimum_quote_unscaled_str: str
    usd_value_str: str
    live_balance_raw: int
    normalized_balance: str
    quote_amount_str: str
    start_price_buffer_bps: int
    min_price_buffer_bps: int
    step_decay_rate_bps: int
    pricing_profile_name: str
    settle_token: str | None = None
    recovery_plan: KickRecoveryPlan | None = None
    quote_response_json: str | None = None
    want_price_usd_str: str | None = None

    @property
    def starting_price_str(self) -> str:
        return self.starting_price_unscaled_str

    @property
    def minimum_price_str(self) -> str:
        return self.minimum_price_scaled_1e18_str

    @property
    def minimum_quote_str(self) -> str:
        return self.minimum_quote_unscaled_str

    @property
    def quote_rate(self) -> str:
        return str(Decimal(self.quote_amount_str) / Decimal(self.normalized_balance))

    @property
    def start_rate(self) -> str:
        return str(Decimal(self.starting_price_unscaled) / Decimal(self.normalized_balance))

    @property
    def floor_rate(self) -> str | None:
        if self.minimum_price_scaled_1e18 is None:
            return None
        return str(Decimal(self.minimum_price_scaled_1e18) / _WAD)


@dataclass(slots=True)
class KickDecision:
    """Evaluator output — whether to kick and why."""

    candidate: KickCandidate
    action: KickAction
    skip_reason: SkipReason | None = None
    detail: str | None = None


@dataclass(slots=True)
class PreparedSweepAndSettle:
    """Prepared stuck-auction abort operation."""

    candidate: KickCandidate
    sell_token: str
    minimum_price_scaled_1e18: int | None
    minimum_price_public_raw: int | None
    available_raw: int | None
    sell_amount_str: str | None
    minimum_price_scaled_1e18_str: str | None
    minimum_price_public_str: str | None
    usd_value_str: str | None
    normalized_balance: str | None
    stuck_abort_reason: str
    token_symbol: str | None = None

    @property
    def minimum_price_str(self) -> str | None:
        return self.minimum_price_scaled_1e18_str


@dataclass(slots=True)
class AuctionInspection:
    """Live auction status snapshot for a candidate auction."""

    auction_address: str
    is_active_auction: bool | None
    active_tokens: tuple[str, ...]
    active_token: str | None = None
    active_available_raw: int | None = None
    active_price_public_raw: int | None = None
    minimum_price_scaled_1e18: int | None = None
    minimum_price_public_raw: int | None = None
    want_address: str | None = None
    want_decimals: int | None = None

    @property
    def active_price_raw(self) -> int | None:
        return self.active_price_public_raw

    @property
    def minimum_price_raw(self) -> int | None:
        return self.minimum_price_scaled_1e18


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
    minimum_quote: str | None = None
    live_balance_raw: int | None = None
    usd_value: str | None = None
    execution_report: TransactionExecutionReport | None = None


@dataclass(slots=True)
class TransactionExecutionReport:
    """Rendered transaction outcome for immediate CLI feedback."""

    operation: str
    sender: str | None
    tx_hash: str
    broadcast_at: str
    chain_id: int
    gas_estimate: int | None = None
    receipt_status: str | None = None
    block_number: int | None = None
    gas_used: int | None = None


@dataclass(slots=True)
class TxnRunResult:
    """Summary of a single evaluation cycle."""

    run_id: str
    status: str
    candidates_found: int
    kicks_attempted: int
    kicks_succeeded: int
    kicks_failed: int
    eligible_candidates_found: int | None = None
    deferred_same_auction_count: int = 0
    limited_candidate_count: int = 0
    failure_summary: dict[str, int] | None = None
