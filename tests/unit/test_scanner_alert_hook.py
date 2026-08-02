from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tidal.scanner.service import ScannerService
from tidal.types import ScanRunResult


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _RunRepository:
    def __init__(self) -> None:
        self.current_status = "RUNNING"
        self.finalized: list[dict[str, object]] = []

    def create(self, row: dict[str, object]) -> None:
        self.current_status = str(row["status"])

    def status(self, run_id: str) -> str:
        del run_id
        return self.current_status

    def finalize(self, run_id: str, **values: object) -> None:
        self.current_status = str(values["status"])
        self.finalized.append({"run_id": run_id, **values})


class _AlertService:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self):  # noqa: ANN201
        self.calls += 1
        return SimpleNamespace(transitions=())


def _scanner(run_side_effect):  # noqa: ANN001, ANN202
    scanner = object.__new__(ScannerService)
    scanner.session = _Session()
    scanner.scan_run_repository = _RunRepository()
    scanner.alert_service = _AlertService()
    scanner.alert_dispatcher = SimpleNamespace(dispatch=AsyncMock())
    scanner._run_scan = (
        AsyncMock(side_effect=run_side_effect)
        if isinstance(run_side_effect, BaseException)
        else AsyncMock(return_value=run_side_effect)
    )
    return scanner


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL_SUCCESS"])
async def test_completed_scans_use_one_post_commit_alert_hook(status: str) -> None:
    result = ScanRunResult("run", status, 1, 1, 1, 1, 0)
    scanner = _scanner(result)

    assert (await scanner.scan_once()).status == status
    assert scanner.alert_service.calls == 1
    scanner.alert_dispatcher.dispatch.assert_awaited_once_with(())


@pytest.mark.asyncio
async def test_raised_scan_exception_finalizes_and_uses_same_alert_hook() -> None:
    scanner = _scanner(RuntimeError("discovery unavailable"))

    with pytest.raises(RuntimeError, match="discovery unavailable"):
        await scanner.scan_once()

    assert scanner.scan_run_repository.current_status == "FAILED"
    assert len(scanner.scan_run_repository.finalized) == 1
    assert scanner.alert_service.calls == 1
    scanner.alert_dispatcher.dispatch.assert_awaited_once_with(())
