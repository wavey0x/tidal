import pytest

from tidal.cli_exit_codes import EXECUTION_ERROR, NOOP, PARTIAL_FAILURE, SUCCESS
from tidal.execution_result import summarize_execution


@pytest.mark.parametrize("statuses,expected_count,status,exit_code", [
    ([], 0, "noop", NOOP),
    ([], 1, "failed", EXECUTION_ERROR),
    (["CONFIRMED"], 1, "confirmed", SUCCESS),
    (["REVERTED"], 1, "failed", EXECUTION_ERROR),
    ([None], 1, "pending", EXECUTION_ERROR),
    (["CONFIRMED", "REVERTED"], 2, "partial", PARTIAL_FAILURE),
    (["CONFIRMED", None], 3, "partial", PARTIAL_FAILURE),
    (["CONFIRMED"], 2, "partial", PARTIAL_FAILURE),
    (["unknown"], 1, "pending", EXECUTION_ERROR),
])
def test_execution_outcome_requires_receipts(statuses, expected_count, status, exit_code):
    result = summarize_execution([{"receiptStatus": item} for item in statuses], expected_count=expected_count)
    assert result.status == status
    assert result.exit_code == exit_code
    assert result.confirmed + result.pending + result.failed + result.unsubmitted == expected_count
