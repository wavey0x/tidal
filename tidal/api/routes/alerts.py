"""Public read-only operational Alerts route."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from tidal.alerts.service import AlertService
from tidal.api.dependencies import get_session, get_settings
from tidal.config import Settings
from tidal.security import redact_sensitive_data

router = APIRouter()


@router.get("/alerts")
def get_alerts(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    evaluation = AlertService(session=session, settings=settings).evaluate()
    return {
        "status": "ok",
        "warnings": [],
        "data": redact_sensitive_data(evaluation.api_payload()),
    }
