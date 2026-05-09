"""Types for the transaction service."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from tidal.auction_price_units import format_buffer_pct

_WAD = Decimal(10) ** 18


SourceType = Literal["strategy", "fee_burner"]
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
class PreparedResolveAuction:
    """Prepared lot-resolution operation emitted before a later kick."""

    candidate: KickCandidate
    sell_token: str
    path: int
    reason: str
    balance_raw: int
    requires_force: bool
    receiver: str | None = None
    token_symbol: str | None = None
    normalized_balance: str | None = None


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
    enabled_tokens: tuple[str, ...] = ()
    inactive_tokens_with_balance: tuple[str, ...] = ()
    inactive_tokens_with_kick: tuple[str, ...] = ()
    candidate_tokens: tuple[str, ...] = ()
    inactive_token: str | None = None
    inactive_token_balance_raw: int | None = None
    inactive_token_kickable_raw: int | None = None
    inactive_token_kicked_at: int | None = None
    auction_length_seconds: int | None = None
    selected_token: str | None = None
    selected_token_active: bool | None = None
    selected_token_balance_raw: int | None = None
    selected_token_kicked_at: int | None = None

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
    quote_response_json: str | None = None
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
class TxIntent:
    """Unsigned transaction prepared for client or server execution."""

    operation: str
    to: str
    data: str
    chain_id: int
    sender: str | None = None
    value: str = "0x0"
    gas_estimate: int | None = None
    gas_limit: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "to": self.to,
            "data": self.data,
            "value": self.value,
            "chainId": self.chain_id,
            "sender": self.sender,
            "gasEstimate": self.gas_estimate,
            "gasLimit": self.gas_limit,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TxIntent":
        return cls(
            operation=str(payload["operation"]),
            to=str(payload["to"]),
            data=str(payload["data"]),
            value=str(payload.get("value") or "0x0"),
            chain_id=int(payload["chainId"]),
            sender=str(payload["sender"]) if payload.get("sender") is not None else None,
            gas_estimate=int(payload["gasEstimate"]) if payload.get("gasEstimate") is not None else None,
            gas_limit=int(payload["gasLimit"]) if payload.get("gasLimit") is not None else None,
        )


@dataclass(slots=True)
class SkippedPreparedCandidate:
    """Candidate filtered out after live prepare or gas-estimation checks."""

    candidate: KickCandidate
    reason: str
    result: KickResult | None = None
    blocked_token_address: str | None = None
    blocked_token_symbol: str | None = None
    blocked_reason: str | None = None
    next_step: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sourceAddress": self.candidate.source_address,
            "sourceName": self.candidate.source_name,
            "auctionAddress": self.candidate.auction_address,
            "tokenAddress": self.candidate.token_address,
            "tokenSymbol": self.candidate.token_symbol,
            "wantSymbol": self.candidate.want_symbol,
            "reason": self.reason,
        }
        if self.blocked_token_address is not None:
            payload["blockedTokenAddress"] = self.blocked_token_address
        if self.blocked_token_symbol is not None:
            payload["blockedTokenSymbol"] = self.blocked_token_symbol
        if self.blocked_reason is not None:
            payload["blockedReason"] = self.blocked_reason
        if self.next_step is not None:
            payload["nextStep"] = self.next_step
        return payload


def _prepared_resolve_preview_item(item: PreparedResolveAuction) -> dict[str, object]:
    return {
        "operation": "resolve-auction",
        "auctionAddress": item.candidate.auction_address,
        "sourceAddress": item.candidate.source_address,
        "sourceName": item.candidate.source_name,
        "sourceType": item.candidate.source_type,
        "tokenAddress": item.sell_token,
        "tokenSymbol": item.token_symbol,
        "wantAddress": item.candidate.want_address,
        "wantSymbol": item.candidate.want_symbol,
        "reason": item.reason,
        "path": item.path,
        "requiresForce": item.requires_force,
        "balanceRaw": str(item.balance_raw),
        "normalizedBalance": item.normalized_balance,
        "receiver": item.receiver,
    }


def _prepared_kick_preview_item(item: PreparedKick) -> dict[str, object]:
    return {
        "operation": "kick",
        "auctionAddress": item.candidate.auction_address,
        "sourceAddress": item.candidate.source_address,
        "sourceName": item.candidate.source_name,
        "sourceType": item.candidate.source_type,
        "tokenAddress": item.candidate.token_address,
        "tokenSymbol": item.candidate.token_symbol,
        "wantAddress": item.candidate.want_address,
        "wantSymbol": item.candidate.want_symbol,
        "wantPriceUsd": item.want_price_usd_str,
        "sellAmount": item.normalized_balance,
        "startingPrice": item.starting_price_unscaled_str,
        "startingPriceDisplay": (
            f"{item.starting_price_unscaled:,} {item.candidate.want_symbol or 'want-token'} "
            f"(+{format_buffer_pct(item.start_price_buffer_bps)} buffer)"
        ),
        "minimumPrice": item.minimum_price_str,
        "minimumPriceDisplay": f"{item.minimum_price_scaled_1e18:,} (scaled 1e18 floor)",
        "minimumQuote": item.minimum_quote_unscaled_str,
        "minimumQuoteDisplay": (
            f"{item.minimum_quote_unscaled:,} {item.candidate.want_symbol or 'want-token'} "
            f"(-{format_buffer_pct(item.min_price_buffer_bps)} buffer)"
        ),
        "minimumPriceScaled1e18": item.minimum_price_scaled_1e18_str,
        "quoteAmount": item.quote_amount_str,
        "quoteResponseJson": item.quote_response_json,
        "usdValue": item.usd_value_str,
        "bufferBps": item.start_price_buffer_bps,
        "minBufferBps": item.min_price_buffer_bps,
        "pricingProfileName": item.pricing_profile_name,
        "stepDecayRateBps": item.step_decay_rate_bps,
        "quoteRate": item.quote_rate,
        "startRate": item.start_rate,
        "floorRate": item.floor_rate,
    }


def _canonical_operation_name(value: object) -> str:
    return str(value or "").strip().replace("-", "_")


def _with_prepared_operation_tx_indexes(
    prepared_operations: list[dict[str, object]],
    tx_intents: list[TxIntent],
) -> list[dict[str, object]]:
    intent_indexes_by_operation: dict[str, list[int]] = {}
    for tx_index, intent in enumerate(tx_intents):
        operation = _canonical_operation_name(intent.operation)
        intent_indexes_by_operation.setdefault(operation, []).append(tx_index)

    prepared_counts: dict[str, int] = {}
    output: list[dict[str, object]] = []
    for operation in prepared_operations:
        operation_with_index = dict(operation)
        operation_name = _canonical_operation_name(operation.get("operation"))
        tx_indexes = intent_indexes_by_operation.get(operation_name, [])
        if tx_indexes:
            if len(tx_indexes) == 1:
                operation_with_index["txIndex"] = tx_indexes[0]
            else:
                prepared_index = prepared_counts.get(operation_name, 0)
                operation_with_index["txIndex"] = tx_indexes[min(prepared_index, len(tx_indexes) - 1)]
                prepared_counts[operation_name] = prepared_index + 1
        output.append(operation_with_index)
    return output


@dataclass(slots=True)
class KickPlan:
    """Single internal representation of a prepared kick action."""

    source_type: str | None
    source_address: str | None
    auction_address: str | None
    token_address: str | None
    limit: int | None
    eligible_count: int
    selected_count: int
    ready_count: int
    ignored_skips: list[dict[str, object]] = field(default_factory=list)
    cooldown_skips: list[dict[str, object]] = field(default_factory=list)
    deferred_same_auction_count: int = 0
    limited_count: int = 0
    ranked_candidates: list[KickCandidate] = field(default_factory=list)
    kick_operations: list[PreparedKick] = field(default_factory=list)
    resolve_operations: list[PreparedResolveAuction] = field(default_factory=list)
    tx_intents: list[TxIntent] = field(default_factory=list)
    skipped_during_prepare: list[SkippedPreparedCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def status(self) -> Literal["ok", "noop"]:
        return "ok" if self.tx_intents else "noop"

    def prepared_operations_preview(self) -> list[dict[str, object]]:
        operations = [
            *[_prepared_resolve_preview_item(item) for item in self.resolve_operations],
            *[_prepared_kick_preview_item(item) for item in self.kick_operations],
        ]
        return _with_prepared_operation_tx_indexes(operations, self.tx_intents)

    def skipped_during_prepare_payload(self) -> list[dict[str, object]]:
        return [item.to_payload() for item in self.skipped_during_prepare]

    def to_preview_payload(self) -> dict[str, object]:
        return {
            "sourceType": self.source_type,
            "sourceAddress": self.source_address,
            "auctionAddress": self.auction_address,
            "tokenAddress": self.token_address,
            "limit": self.limit,
            "eligibleCount": self.eligible_count,
            "selectedCount": self.selected_count,
            "readyCount": self.ready_count,
            "ignoredCount": len(self.ignored_skips),
            "ignoredSkips": self.ignored_skips,
            "deferredSameAuctionCount": self.deferred_same_auction_count,
            "limitedCount": self.limited_count,
            "cooldownCount": len(self.cooldown_skips),
            "cooldownSkips": self.cooldown_skips,
            "skippedDuringPrepare": self.skipped_during_prepare_payload(),
            "preparedOperations": self.prepared_operations_preview(),
        }

    def to_transaction_payloads(self) -> list[dict[str, object]]:
        return [intent.to_payload() for intent in self.tx_intents]


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
