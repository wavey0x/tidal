"""Receipt-based execution summaries shared by operator commands."""

from dataclasses import dataclass
from typing import Mapping, Sequence

from tidal.cli_exit_codes import EXECUTION_ERROR, NOOP, PARTIAL_FAILURE, SUCCESS


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    confirmed: int
    pending: int
    failed: int
    unsubmitted: int

    @property
    def exit_code(self) -> int:
        return {
            "confirmed": SUCCESS,
            "noop": NOOP,
            "partial": PARTIAL_FAILURE,
            "pending": EXECUTION_ERROR,
            "failed": EXECUTION_ERROR,
        }[self.status]


def summarize_execution(records: Sequence[Mapping[str, object]], *, expected_count: int) -> ExecutionResult:
    confirmed = sum(record.get("receiptStatus") == "CONFIRMED" for record in records)
    failed = sum(record.get("receiptStatus") in {"REVERTED", "FAILED"} for record in records)
    pending = len(records) - confirmed - failed
    unsubmitted = max(0, expected_count - len(records))
    if not records and expected_count == 0:
        status = "noop"
    elif confirmed and not (pending or failed or unsubmitted):
        status = "confirmed"
    elif confirmed or (pending and failed):
        status = "partial"
    elif pending:
        status = "pending"
    else:
        status = "failed"
    return ExecutionResult(status, confirmed, pending, failed, unsubmitted)
