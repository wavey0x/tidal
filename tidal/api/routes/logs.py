"""Log and run-history routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from tidal.api.dependencies import get_session, get_settings
from tidal.api.errors import APIError
from tidal.config import Settings
from tidal.read.kick_logs import KickLogReadService
from tidal.read.run_logs import RunLogReadService
from tidal.read.scan_logs import ScanLogReadService
from tidal.security import redact_sensitive_data

router = APIRouter()


@router.get("/logs/kicks")
def get_kick_logs(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    source: str | None = Query(default=None),
    auction: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    kick_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    service = KickLogReadService(session, chain_id=settings.chain_id, auctionscan_base_url=settings.auctionscan_base_url)
    data = service.list_kicks(
        limit=limit,
        offset=offset,
        status=status,
        q=q,
        source_address=source,
        auction_address=auction,
        run_id=run_id,
        kick_id=kick_id,
    )
    return {
        "status": "ok" if data["kicks"] else "noop",
        "warnings": [],
        "data": redact_sensitive_data(data),
    }


@router.get("/logs/scans")
def get_scan_logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    data = ScanLogReadService(session).list_runs(limit=limit, offset=offset, status=status)
    return {
        "status": "ok" if data["items"] else "noop",
        "warnings": [],
        "data": redact_sensitive_data(data),
    }


@router.get("/logs/runs/{run_id}")
def get_run_detail(
    run_id: str,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    detail = RunLogReadService(session).get_detail(run_id)
    if detail is None:
        raise APIError("Run not found", status_code=404)
    return {"status": "ok", "warnings": [], "data": redact_sensitive_data(detail)}
