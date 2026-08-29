"""Canonical auction-round classification and bounded no-fill retry policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence

from tidal.normalizers import normalize_address


ROUND_CLOSING_PATHS = frozenset({1, 3, 4, 5})
ROUND_RESOLUTION_PATHS = frozenset(range(6))


class RoundOutcome(str, Enum):
    NO_FILL = "NO_FILL"
    PRODUCTIVE = "PRODUCTIVE"
    INCOMPLETE = "INCOMPLETE"
    UNKNOWN = "UNKNOWN"


class NoFillAction(str, Enum):
    ALLOW = "ALLOW"
    DEFER = "DEFER"
    BLOCK = "BLOCK"


class NoFillReason(str, Enum):
    INITIAL = "INITIAL"
    PRODUCTIVE_RESET = "PRODUCTIVE_RESET"
    RETRY_DUE = "RETRY_DUE"
    RETRY_NOT_DUE = "RETRY_NOT_DUE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    ROUND_INCOMPLETE = "ROUND_INCOMPLETE"
    MANUAL_RETRY_OVERRIDE = "MANUAL_RETRY_OVERRIDE"


@dataclass(frozen=True, slots=True)
class RoundEvidence:
    outcome: RoundOutcome
    reason_code: str
    kick_id: int
    recovery_ids: tuple[int, ...] = ()
    close_id: int | None = None
    requested_amount: int | None = None
    placed_amount: int | None = None
    recovered_amount: int | None = None
    kick_position: tuple[int, int] | None = None
    close_position: tuple[int, int] | None = None
    kick_at: datetime | None = None
    close_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RoundSequence:
    rounds: tuple[RoundEvidence, ...]
    consecutive_no_fills: int
    terminal_outcome: RoundOutcome | None

    @property
    def latest(self) -> RoundEvidence | None:
        return self.rounds[0] if self.rounds else None

    @property
    def no_fill_rounds(self) -> tuple[RoundEvidence, ...]:
        return tuple(
            round_ for round_ in self.rounds if round_.outcome == RoundOutcome.NO_FILL
        )


@dataclass(frozen=True, slots=True)
class NoFillDecision:
    action: NoFillAction
    reason_code: NoFillReason
    auction_address: str
    token_address: str
    consecutive_no_fills: int
    retry_ordinal: int | None
    retry_total: int
    retry_at: datetime | None
    first_evidence_at: datetime | None
    latest_evidence_at: datetime | None
    kick_ids: tuple[int, ...]
    recovery_ids: tuple[int, ...]
    sequence: RoundSequence


@dataclass(frozen=True, slots=True)
class NoFillSuspensionClearPlan:
    baseline_kick_id: int
    newer_kick_ids: tuple[int, ...]


def _text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    return str(value) if value is not None else None


def _raw_amount(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        amount = int(str(value))
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _timestamp(row: Mapping[str, object], key: str) -> datetime | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _position(row: Mapping[str, object]) -> tuple[int, int] | None:
    block_number = row.get("block_number")
    transaction_index = row.get("transaction_index")
    if block_number is None or transaction_index is None:
        return None
    try:
        return int(block_number), int(transaction_index)
    except (TypeError, ValueError):
        return None


def _same_pair(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    try:
        return normalize_address(str(left.get("auction_address"))) == normalize_address(
            str(right.get("auction_address"))
        ) and normalize_address(str(left.get("token_address"))) == normalize_address(
            str(right.get("token_address"))
        )
    except Exception:
        return False


def operation_closes_round(row: Mapping[str, object]) -> bool:
    operation_type = _text(row, "operation_type")
    if operation_type == "auction_settled":
        return True
    if operation_type != "resolve_auction":
        return False
    try:
        return int(row.get("resolution_path")) in ROUND_CLOSING_PATHS
    except (TypeError, ValueError):
        return False


def _unknown(
    kick: Mapping[str, object], reason: str, **kwargs: object
) -> RoundEvidence:
    return RoundEvidence(
        outcome=RoundOutcome.UNKNOWN,
        reason_code=reason,
        kick_id=int(kick["id"]),
        kick_position=_position(kick),
        kick_at=_timestamp(kick, "mined_at"),
        **kwargs,
    )


def _incomplete(
    kick: Mapping[str, object], reason: str, **kwargs: object
) -> RoundEvidence:
    return RoundEvidence(
        outcome=RoundOutcome.INCOMPLETE,
        reason_code=reason,
        kick_id=int(kick["id"]),
        kick_position=_position(kick),
        kick_at=_timestamp(kick, "mined_at"),
        **kwargs,
    )


def classify_round(
    kick: Mapping[str, object],
    linked_operations: Sequence[Mapping[str, object]],
) -> RoundEvidence:
    """Classify one kick using only exact, explicitly linked chain evidence."""

    kick_id = int(kick["id"])
    if _text(kick, "operation_type") != "kick":
        return _unknown(kick, "NOT_A_KICK")
    if _text(kick, "status") == "SUBMITTED":
        return _incomplete(kick, "KICK_SUBMITTED")
    if _text(kick, "status") != "CONFIRMED":
        return _unknown(kick, "KICK_NOT_CONFIRMED")

    linked = [row for row in linked_operations if row.get("round_kick_id") == kick_id]
    if any(_text(row, "status") == "SUBMITTED" for row in linked):
        return _incomplete(kick, "RECOVERY_SUBMITTED")

    kick_position = _position(kick)
    kick_at = _timestamp(kick, "mined_at")
    requested = _raw_amount(kick, "requested_sell_amount")
    placed = _raw_amount(kick, "sell_amount")
    if requested is None:
        return _unknown(kick, "MISSING_REQUESTED_AMOUNT")
    if placed is None or placed <= 0:
        return _unknown(kick, "INVALID_PLACED_AMOUNT", requested_amount=requested)
    if requested != placed:
        return _unknown(
            kick,
            "REQUESTED_PLACED_MISMATCH",
            requested_amount=requested,
            placed_amount=placed,
        )
    if kick_position is None or kick_at is None:
        return _unknown(
            kick,
            "MISSING_KICK_CHAIN_POSITION",
            requested_amount=requested,
            placed_amount=placed,
        )

    confirmed: list[Mapping[str, object]] = []
    for row in linked:
        if not _same_pair(kick, row):
            return _unknown(
                kick,
                "ROUND_PAIR_MISMATCH",
                requested_amount=requested,
                placed_amount=placed,
            )
        if _text(row, "status") != "CONFIRMED":
            continue
        if _position(row) is None or _timestamp(row, "mined_at") is None:
            return _unknown(
                kick,
                "MISSING_RECOVERY_CHAIN_POSITION",
                requested_amount=requested,
                placed_amount=placed,
            )
        if _position(row) <= kick_position:  # type: ignore[operator]
            return _unknown(
                kick,
                "RECOVERY_BEFORE_KICK",
                requested_amount=requested,
                placed_amount=placed,
            )
        confirmed.append(row)

    sweeps = [
        row for row in confirmed if _text(row, "operation_type") == "sweep_auction"
    ]
    resolves = [
        row for row in confirmed if _text(row, "operation_type") == "resolve_auction"
    ]
    settlements = [
        row for row in confirmed if _text(row, "operation_type") == "auction_settled"
    ]

    resolved_paths: list[tuple[Mapping[str, object], int]] = []
    for row in resolves:
        try:
            path = int(row.get("resolution_path"))
        except (TypeError, ValueError):
            return _unknown(
                kick,
                "MISSING_RESOLUTION_PATH",
                requested_amount=requested,
                placed_amount=placed,
            )
        if path not in ROUND_RESOLUTION_PATHS:
            return _unknown(
                kick,
                "INVALID_RESOLUTION_PATH",
                requested_amount=requested,
                placed_amount=placed,
            )
        resolved_paths.append((row, path))

    recovered = 0
    recovery_ids: list[int] = []
    non_closing_recoveries = [row for row, path in resolved_paths if path == 2]
    for row in [*sweeps, *non_closing_recoveries]:
        amount = _raw_amount(row, "sell_amount")
        if amount is None:
            return _unknown(
                kick,
                "MISSING_RECOVERED_AMOUNT",
                requested_amount=requested,
                placed_amount=placed,
            )
        recovered += amount
        recovery_ids.append(int(row["id"]))

    closes = [
        *(row for row, path in resolved_paths if path in ROUND_CLOSING_PATHS),
        *settlements,
    ]
    if len(closes) == 0:
        return _incomplete(
            kick,
            "MISSING_LOGICAL_CLOSE",
            requested_amount=requested,
            placed_amount=placed,
            recovery_ids=tuple(recovery_ids),
        )
    if len(closes) != 1:
        return _unknown(
            kick,
            "DUPLICATE_LOGICAL_CLOSE",
            requested_amount=requested,
            placed_amount=placed,
            recovery_ids=tuple([*recovery_ids, *(int(row["id"]) for row in closes)]),
        )

    close = closes[0]
    close_position = _position(close)
    close_at = _timestamp(close, "mined_at")
    assert close_position is not None and close_at is not None
    if any(_position(row) > close_position for row in sweeps):  # type: ignore[operator]
        return _unknown(
            kick,
            "RECOVERY_AFTER_CLOSE",
            requested_amount=requested,
            placed_amount=placed,
        )

    close_amount = _raw_amount(close, "sell_amount")
    if close_amount is None:
        return _unknown(
            kick,
            "MISSING_CLOSE_AMOUNT",
            requested_amount=requested,
            placed_amount=placed,
        )
    if _text(close, "operation_type") == "auction_settled" and close_amount != 0:
        return _unknown(
            kick,
            "INVALID_SETTLEMENT_AMOUNT",
            requested_amount=requested,
            placed_amount=placed,
        )
    recovered += close_amount
    recovery_ids.append(int(close["id"]))
    if recovered > placed:
        return _unknown(
            kick,
            "RECOVERY_EXCEEDS_PLACED",
            requested_amount=requested,
            placed_amount=placed,
            recovered_amount=recovered,
            recovery_ids=tuple(recovery_ids),
            close_id=int(close["id"]),
            close_position=close_position,
            close_at=close_at,
        )

    outcome = RoundOutcome.NO_FILL if recovered == placed else RoundOutcome.PRODUCTIVE
    return RoundEvidence(
        outcome=outcome,
        reason_code="EXACT_FULL_RECOVERY"
        if outcome == RoundOutcome.NO_FILL
        else "POSITIVE_FILL",
        kick_id=kick_id,
        recovery_ids=tuple(recovery_ids),
        close_id=int(close["id"]),
        requested_amount=requested,
        placed_amount=placed,
        recovered_amount=recovered,
        kick_position=kick_position,
        close_position=close_position,
        kick_at=kick_at,
        close_at=close_at,
    )


def _classify_pair_rounds(
    rows: Sequence[Mapping[str, object]],
) -> tuple[RoundEvidence, ...]:
    kicks = [
        row
        for row in rows
        if _text(row, "operation_type") == "kick"
        and _text(row, "status") in {"CONFIRMED", "SUBMITTED"}
    ]
    kicks.sort(
        key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)),
        reverse=True,
    )
    if not kicks:
        return ()
    unlinked_closes = [
        row
        for row in rows
        if _text(row, "status") in {"CONFIRMED", "SUBMITTED"}
        and _text(row, "operation_type") in {"resolve_auction", "auction_settled"}
        and row.get("round_kick_id") is None
        and (_text(row, "status") == "SUBMITTED" or operation_closes_round(row))
    ]

    def has_unlinked_close(kick_index: int) -> bool:
        kick = kicks[kick_index]
        kick_position = _position(kick)
        newer_position = _position(kicks[kick_index - 1]) if kick_index > 0 else None
        kick_created = _timestamp(kick, "created_at")
        newer_created = (
            _timestamp(kicks[kick_index - 1], "created_at") if kick_index > 0 else None
        )
        for close in unlinked_closes:
            close_position = _position(close)
            if kick_position is not None and close_position is not None:
                if close_position > kick_position and (
                    newer_position is None or close_position < newer_position
                ):
                    return True
                continue
            close_created = _timestamp(close, "created_at")
            if kick_created is not None and close_created is not None:
                if close_created > kick_created and (
                    newer_created is None or close_created < newer_created
                ):
                    return True
        return False

    classified: list[RoundEvidence] = []
    for index, kick in enumerate(kicks):
        evidence = (
            _unknown(kick, "UNLINKED_LOGICAL_CLOSE")
            if has_unlinked_close(index)
            else classify_round(kick, rows)
        )
        classified.append(evidence)
    return tuple(classified)


def classify_all_pair_operations(
    rows: Sequence[Mapping[str, object]],
) -> tuple[RoundEvidence, ...]:
    """Classify every retained round for one auction/token pair."""

    return _classify_pair_rounds(rows)


def _current_pair_rounds(
    rows: Sequence[Mapping[str, object]],
) -> tuple[RoundEvidence, ...]:
    classified = _classify_pair_rounds(rows)
    baselined_kick_ids = {
        int(row["id"])
        for row in rows
        if _text(row, "operation_type") == "kick"
        and int(row.get("historical_baseline") or 0) == 1
    }
    baseline_index = next(
        (
            index
            for index, evidence in enumerate(classified)
            if evidence.kick_id in baselined_kick_ids
        ),
        None,
    )
    return classified if baseline_index is None else classified[:baseline_index]


def plan_no_fill_suspension_clear(
    rows: Sequence[Mapping[str, object]],
) -> NoFillSuspensionClearPlan:
    """Select the newest completed no-fill that can safely reset pair history."""

    current_rounds = _current_pair_rounds(rows)
    newer_kick_ids: list[int] = []
    for evidence in current_rounds:
        if evidence.outcome == RoundOutcome.NO_FILL:
            return NoFillSuspensionClearPlan(
                baseline_kick_id=evidence.kick_id,
                newer_kick_ids=tuple(newer_kick_ids),
            )
        if evidence.outcome == RoundOutcome.INCOMPLETE:
            newer_kick_ids.append(evidence.kick_id)
            continue
        if evidence.outcome == RoundOutcome.UNKNOWN:
            raise ValueError(
                f"cannot clear across ambiguous round {evidence.kick_id}: {evidence.reason_code}"
            )
        raise ValueError(
            "no no-fill suspension to clear after the latest productive round"
        )
    raise ValueError("no completed no-fill round is available to clear")


def classify_pair_operations(rows: Sequence[Mapping[str, object]]) -> RoundSequence:
    """Classify the latest contiguous sequence after any reviewed baseline."""

    current_rounds = _current_pair_rounds(rows)
    sequence: list[RoundEvidence] = []
    consecutive_no_fills = 0
    terminal: RoundOutcome | None = None
    for evidence in current_rounds:
        sequence.append(evidence)
        terminal = evidence.outcome
        if evidence.outcome == RoundOutcome.NO_FILL:
            consecutive_no_fills += 1
            continue
        break

    return RoundSequence(
        rounds=tuple(sequence),
        consecutive_no_fills=consecutive_no_fills,
        terminal_outcome=terminal,
    )


class NoFillGuard:
    """Convert canonical pair history into an enforcement decision."""

    def __init__(self, kick_tx_repository, retry_delays_minutes: Sequence[int]):  # noqa: ANN001
        self.kick_tx_repository = kick_tx_repository
        self.retry_delays_minutes = tuple(int(value) for value in retry_delays_minutes)

    def decide(
        self,
        *,
        auction_address: str,
        token_address: str,
        now: datetime | None = None,
        allow_exhausted_retry: bool = False,
    ) -> NoFillDecision:
        normalized_auction = normalize_address(auction_address)
        normalized_token = normalize_address(token_address)
        sequence = classify_pair_operations(
            self.kick_tx_repository.list_pair_operations(
                normalized_auction, normalized_token
            )
        )
        retry_total = len(self.retry_delays_minutes)
        evidence_times = [
            value
            for round_ in sequence.rounds
            for value in (round_.kick_at, round_.close_at)
            if value is not None
        ]
        kick_ids = tuple(round_.kick_id for round_ in sequence.rounds)
        recovery_ids = tuple(
            row_id for round_ in sequence.rounds for row_id in round_.recovery_ids
        )

        def decision(
            action: NoFillAction,
            reason: NoFillReason,
            *,
            retry_ordinal: int | None = None,
            retry_at: datetime | None = None,
        ) -> NoFillDecision:
            return NoFillDecision(
                action=action,
                reason_code=reason,
                auction_address=normalized_auction,
                token_address=normalized_token,
                consecutive_no_fills=sequence.consecutive_no_fills,
                retry_ordinal=retry_ordinal,
                retry_total=retry_total,
                retry_at=retry_at,
                first_evidence_at=min(evidence_times) if evidence_times else None,
                latest_evidence_at=max(evidence_times) if evidence_times else None,
                kick_ids=kick_ids,
                recovery_ids=recovery_ids,
                sequence=sequence,
            )

        latest = sequence.latest
        if latest is None:
            return decision(NoFillAction.ALLOW, NoFillReason.INITIAL)
        if latest.outcome == RoundOutcome.PRODUCTIVE:
            return decision(NoFillAction.ALLOW, NoFillReason.PRODUCTIVE_RESET)
        barrier = next(
            (
                round_
                for round_ in sequence.rounds
                if round_.outcome in {RoundOutcome.UNKNOWN, RoundOutcome.INCOMPLETE}
            ),
            None,
        )
        if barrier is not None and barrier.outcome == RoundOutcome.UNKNOWN:
            return decision(NoFillAction.BLOCK, NoFillReason.OUTCOME_UNKNOWN)
        if barrier is not None:
            return decision(NoFillAction.BLOCK, NoFillReason.ROUND_INCOMPLETE)

        count = sequence.consecutive_no_fills
        if count > retry_total:
            if allow_exhausted_retry:
                return decision(NoFillAction.ALLOW, NoFillReason.MANUAL_RETRY_OVERRIDE)
            return decision(NoFillAction.BLOCK, NoFillReason.RETRY_EXHAUSTED)

        latest_close = latest.close_at
        if latest_close is None:
            return decision(NoFillAction.BLOCK, NoFillReason.OUTCOME_UNKNOWN)
        retry_at = latest_close + timedelta(
            minutes=self.retry_delays_minutes[count - 1]
        )
        retry_ordinal = count
        current_time = now or datetime.now(timezone.utc)
        if current_time.astimezone(timezone.utc) >= retry_at:
            return decision(
                NoFillAction.ALLOW,
                NoFillReason.RETRY_DUE,
                retry_ordinal=retry_ordinal,
                retry_at=retry_at,
            )
        return decision(
            NoFillAction.DEFER,
            NoFillReason.RETRY_NOT_DUE,
            retry_ordinal=retry_ordinal,
            retry_at=retry_at,
        )
