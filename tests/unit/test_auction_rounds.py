from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tidal.auction_rounds import (
    NoFillAction,
    NoFillGuard,
    NoFillReason,
    RoundOutcome,
    classify_pair_operations,
    classify_round,
)


AUCTION = "0x00000000000000000000000000000000000000a1"
TOKEN = "0x00000000000000000000000000000000000000b1"
NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def row(
    row_id: int,
    operation_type: str,
    *,
    status: str = "CONFIRMED",
    sell_amount: str | None = "100",
    requested_sell_amount: str | None = None,
    round_kick_id: int | None = None,
    resolution_path: int | None = None,
    hour: int = 0,
    block_number: int | None = None,
    transaction_index: int = 0,
) -> dict[str, object]:
    mined = NOW + timedelta(hours=hour)
    return {
        "id": row_id,
        "run_id": f"run-{row_id}",
        "operation_type": operation_type,
        "status": status,
        "auction_address": AUCTION,
        "token_address": TOKEN,
        "sell_amount": sell_amount,
        "requested_sell_amount": requested_sell_amount,
        "round_kick_id": round_kick_id,
        "resolution_path": resolution_path,
        "block_number": block_number if block_number is not None else 100 + hour,
        "transaction_index": transaction_index,
        "mined_at": mined.isoformat() if status == "CONFIRMED" else None,
        "created_at": mined.isoformat(),
    }


def kick(
    row_id: int = 1, *, amount: str = "100", hour: int = 0, status: str = "CONFIRMED"
) -> dict[str, object]:
    return row(
        row_id,
        "kick",
        status=status,
        sell_amount=amount if status == "CONFIRMED" else None,
        requested_sell_amount=amount,
        hour=hour,
    )


def resolve(
    row_id: int = 2,
    *,
    recovered: str = "100",
    kick_id: int = 1,
    hour: int = 1,
    path: int = 1,
) -> dict[str, object]:
    return row(
        row_id,
        "resolve_auction",
        sell_amount=recovered,
        round_kick_id=kick_id,
        resolution_path=path,
        hour=hour,
    )


class Repo:
    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows

    def list_pair_operations(
        self, auction_address: str, token_address: str
    ) -> list[dict[str, object]]:
        assert auction_address == AUCTION
        assert token_address == TOKEN
        return self.rows


def test_exact_full_recovery_is_no_fill() -> None:
    evidence = classify_round(kick(), [resolve()])
    assert evidence.outcome == RoundOutcome.NO_FILL
    assert evidence.recovered_amount == 100


def test_zero_or_partial_recovery_is_productive() -> None:
    assert (
        classify_round(kick(), [resolve(recovered="0")]).outcome
        == RoundOutcome.PRODUCTIVE
    )
    assert (
        classify_round(kick(), [resolve(recovered="99")]).outcome
        == RoundOutcome.PRODUCTIVE
    )


def test_requested_and_actual_mismatch_is_unknown() -> None:
    mismatched = kick()
    mismatched["sell_amount"] = "90"
    assert (
        classify_round(mismatched, [resolve(recovered="90")]).reason_code
        == "REQUESTED_PLACED_MISMATCH"
    )


def test_direct_settlement_closes_productively() -> None:
    settled = row(2, "auction_settled", sell_amount="0", round_kick_id=1, hour=1)
    evidence = classify_round(kick(), [settled])
    assert evidence.outcome == RoundOutcome.PRODUCTIVE
    assert evidence.close_id == 2


def test_manual_sweep_then_zero_resolve_sums_recovery() -> None:
    sweep = row(2, "sweep_auction", sell_amount="40", round_kick_id=1, hour=1)
    close = resolve(3, recovered="0", hour=2)
    evidence = classify_round(kick(), [sweep, close])
    assert evidence.outcome == RoundOutcome.PRODUCTIVE
    assert evidence.recovered_amount == 40


def test_non_closing_resolution_paths_do_not_create_false_ambiguity() -> None:
    no_op_then_close = classify_round(
        kick(),
        [
            resolve(2, recovered="0", path=0),
            resolve(3, recovered="0", path=5, hour=2),
        ],
    )
    assert no_op_then_close.outcome == RoundOutcome.PRODUCTIVE
    assert no_op_then_close.close_id == 3

    sweep_then_reset = classify_round(
        kick(),
        [
            resolve(2, recovered="40", path=2),
            resolve(3, recovered="0", path=4, hour=2),
        ],
    )
    assert sweep_then_reset.outcome == RoundOutcome.PRODUCTIVE
    assert sweep_then_reset.recovered_amount == 40


def test_unknown_resolution_path_fails_closed() -> None:
    evidence = classify_round(kick(), [resolve(path=6)])
    assert evidence.outcome == RoundOutcome.UNKNOWN
    assert evidence.reason_code == "INVALID_RESOLUTION_PATH"


@pytest.mark.parametrize(
    ("operations", "reason"),
    [
        ([], "MISSING_LOGICAL_CLOSE"),
        ([resolve(), resolve(3, hour=2)], "DUPLICATE_LOGICAL_CLOSE"),
        ([resolve(recovered="101")], "RECOVERY_EXCEEDS_PLACED"),
    ],
)
def test_incomplete_and_invalid_evidence_fail_closed(
    operations: list[dict[str, object]], reason: str
) -> None:
    evidence = classify_round(kick(), operations)
    assert evidence.reason_code == reason
    if reason == "MISSING_LOGICAL_CLOSE":
        assert evidence.outcome == RoundOutcome.INCOMPLETE
    else:
        assert evidence.outcome == RoundOutcome.UNKNOWN


def test_submitted_recovery_has_incomplete_precedence() -> None:
    submitted = resolve()
    submitted["status"] = "SUBMITTED"
    submitted["sell_amount"] = None
    assert classify_round(kick(), [submitted]).outcome == RoundOutcome.INCOMPLETE


def test_malformed_confirmed_sweep_is_unknown_before_missing_close() -> None:
    sweep = row(2, "sweep_auction", sell_amount=None, round_kick_id=1, hour=1)
    evidence = classify_round(kick(), [sweep])
    assert evidence.outcome == RoundOutcome.UNKNOWN
    assert evidence.reason_code == "MISSING_RECOVERED_AMOUNT"


def test_unlinked_sweep_is_ignored_but_unlinked_close_is_unknown() -> None:
    unlinked_sweep = row(
        3, "sweep_auction", sell_amount="100", round_kick_id=None, hour=2
    )
    sequence = classify_pair_operations([kick(), resolve(), unlinked_sweep])
    assert sequence.terminal_outcome == RoundOutcome.NO_FILL

    unlinked_close = resolve()
    unlinked_close["round_kick_id"] = None
    sequence = classify_pair_operations([kick(), unlinked_close])
    assert sequence.terminal_outcome == RoundOutcome.UNKNOWN


def test_latest_incomplete_stops_before_older_productive_round() -> None:
    old_kick = kick(1, hour=0)
    old_close = resolve(2, recovered="0", kick_id=1, hour=1)
    current = kick(3, hour=2, status="SUBMITTED")
    sequence = classify_pair_operations([old_kick, old_close, current])
    assert sequence.latest is not None
    assert sequence.latest.outcome == RoundOutcome.INCOMPLETE
    assert len(sequence.rounds) == 1


def completed_no_fill(kick_id: int, *, hour: int) -> list[dict[str, object]]:
    return [
        kick(kick_id, hour=hour),
        resolve(kick_id + 1, kick_id=kick_id, hour=hour + 1),
    ]


def test_guard_uses_full_12_and_24_hour_delays_after_close() -> None:
    first_rows = completed_no_fill(1, hour=0)
    guard = NoFillGuard(Repo(first_rows), [720, 1440])
    before = guard.decide(
        auction_address=AUCTION,
        token_address=TOKEN,
        now=NOW + timedelta(hours=12, minutes=59),
    )
    assert before.action == NoFillAction.DEFER
    assert before.retry_at == NOW + timedelta(hours=13)
    due = guard.decide(
        auction_address=AUCTION, token_address=TOKEN, now=NOW + timedelta(hours=13)
    )
    assert due.reason_code == NoFillReason.RETRY_DUE

    second_rows = [*first_rows, *completed_no_fill(3, hour=14)]
    second = NoFillGuard(Repo(second_rows), [720, 1440]).decide(
        auction_address=AUCTION,
        token_address=TOKEN,
        now=NOW + timedelta(hours=38, minutes=59),
    )
    assert second.action == NoFillAction.DEFER
    assert second.retry_at == NOW + timedelta(hours=39)


def test_third_no_fill_blocks_and_only_scoped_override_bypasses_exhaustion() -> None:
    rows = [
        *completed_no_fill(1, hour=0),
        *completed_no_fill(3, hour=14),
        *completed_no_fill(5, hour=40),
    ]
    guard = NoFillGuard(Repo(rows), [720, 1440])
    blocked = guard.decide(
        auction_address=AUCTION, token_address=TOKEN, now=NOW + timedelta(days=4)
    )
    assert blocked.action == NoFillAction.BLOCK
    assert blocked.reason_code == NoFillReason.RETRY_EXHAUSTED
    overridden = guard.decide(
        auction_address=AUCTION,
        token_address=TOKEN,
        now=NOW + timedelta(days=4),
        allow_exhausted_retry=True,
    )
    assert overridden.action == NoFillAction.ALLOW
    assert overridden.reason_code == NoFillReason.MANUAL_RETRY_OVERRIDE


def test_productive_round_resets_older_no_fills() -> None:
    rows = [
        *completed_no_fill(1, hour=0),
        kick(3, hour=14),
        resolve(4, recovered="0", kick_id=3, hour=15),
    ]
    decision = NoFillGuard(Repo(rows), [720, 1440]).decide(
        auction_address=AUCTION,
        token_address=TOKEN,
        now=NOW + timedelta(days=2),
    )
    assert decision.action == NoFillAction.ALLOW
    assert decision.reason_code == NoFillReason.PRODUCTIVE_RESET
    assert decision.consecutive_no_fills == 0


def test_ambiguity_inside_current_no_fill_sequence_blocks_retry() -> None:
    ambiguous_kick = kick(1, hour=0)
    ambiguous_kick["sell_amount"] = "99"
    rows = [
        ambiguous_kick,
        resolve(2, recovered="99", kick_id=1, hour=1),
        *completed_no_fill(3, hour=2),
    ]
    decision = NoFillGuard(Repo(rows), [720, 1440]).decide(
        auction_address=AUCTION,
        token_address=TOKEN,
        now=NOW + timedelta(days=2),
    )
    assert decision.action == NoFillAction.BLOCK
    assert decision.reason_code == NoFillReason.OUTCOME_UNKNOWN
