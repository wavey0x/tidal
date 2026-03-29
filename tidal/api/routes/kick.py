"""Kick read and prepare routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from tidal.api.auth import OperatorIdentity
from tidal.api.dependencies import get_operator, get_session, get_settings
from tidal.api.schemas.kick import KickInspectRequest, KickPrepareRequest
from tidal.api.services.action_prepare import inspect_kicks, prepare_kick_action
from tidal.api.services.auctionscan import AuctionScanService
from tidal.config import Settings

router = APIRouter()


@router.post("/kick/inspect")
def post_kick_inspect(
    payload: KickInspectRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    data = inspect_kicks(
        session,
        settings,
        source_type=payload.source_type,
        source_address=payload.source_address,
        auction_address=payload.auction_address,
        token_address=payload.token_address,
        limit=payload.limit,
        include_live_inspection=payload.include_live_inspection,
    )
    status = "ok" if data.get("ready_count") or data.get("cooldown_count") or data.get("deferred_same_auction_count") else "noop"
    return {"status": status, "warnings": [], "data": data}


@router.post("/kick/prepare")
async def post_kick_prepare(
    payload: KickPrepareRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    status, warnings, data = await prepare_kick_action(
        session,
        settings,
        operator_id=operator.operator_id,
        source_type=payload.source_type,
        source_address=payload.source_address,
        auction_address=payload.auction_address,
        token_address=payload.token_address,
        limit=payload.limit,
        sender=payload.sender,
    )
    return {"status": status, "warnings": warnings, "data": data}


@router.get("/kicks/{kick_id}/auctionscan")
async def get_kick_auctionscan(
    kick_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    data = await AuctionScanService(session, settings).resolve_kick_auctionscan(kick_id)
    return {"status": "ok", "warnings": [], "data": data}
