"""Dashboard routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from tidal.api.dependencies import get_session
from tidal.api.services.dashboard import load_dashboard
from tidal.security import redact_sensitive_data

router = APIRouter()


@router.get("/dashboard")
def get_dashboard(
    session: Session = Depends(get_session),
) -> dict[str, object]:
    return {"status": "ok", "warnings": [], "data": redact_sensitive_data(load_dashboard(session))}
