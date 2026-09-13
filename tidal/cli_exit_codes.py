"""Named CLI exit codes for automation-friendly commands."""

from __future__ import annotations

SUCCESS = 0
NOOP = 2
VALIDATION_ERROR = 3
EXECUTION_ERROR = 4
PARTIAL_FAILURE = 5


def scan_exit_code(status: str) -> int:
    if status == "SUCCESS":
        return SUCCESS
    if status == "PARTIAL_SUCCESS":
        return PARTIAL_FAILURE
    return EXECUTION_ERROR


def kick_exit_code(*, live: bool, status: str, candidates_found: int, kicks_failed: int) -> int:
    if status in {"BUSY", "WAITING"}:
        return 75
    if candidates_found == 0:
        return NOOP
    if not live:
        return SUCCESS
    if status == "PARTIAL_SUCCESS" or (kicks_failed > 0 and status == "SUCCESS"):
        return PARTIAL_FAILURE
    if status == "FAILED":
        return EXECUTION_ERROR
    return SUCCESS


def simple_list_exit_code(count: int) -> int:
    return SUCCESS if count > 0 else NOOP
