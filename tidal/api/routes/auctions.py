"""Auction prepare routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from tidal.api.auth import OperatorIdentity
from tidal.api.dependencies import get_operator, get_session, get_settings
from tidal.api.schemas.auctions import (
    AuctionDeployPrepareRequest,
    AuctionEnableTokensPrepareRequest,
    AuctionSettlePrepareRequest,
)
from tidal.api.services.action_prepare import (
    load_strategy_deploy_defaults,
    prepare_deploy_action,
    prepare_enable_tokens_action,
    prepare_settle_action,
)
from tidal.config import Settings
from tidal.security import redact_sensitive_data

router = APIRouter()


@router.get("/strategies/{strategy}/deploy-defaults")
async def get_deploy_defaults(
    strategy: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    data = await load_strategy_deploy_defaults(session, settings, strategy_address=strategy)
    warnings = redact_sensitive_data(data.pop("warnings", []))
    return {"status": "ok", "warnings": warnings, "data": redact_sensitive_data(data)}


@router.post("/auctions/deploy/prepare")
async def post_deploy_prepare(
    payload: AuctionDeployPrepareRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    status, warnings, data = await prepare_deploy_action(
        settings,
        session,
        operator_id=operator.operator_id,
        want=payload.want,
        receiver=payload.receiver,
        sender=payload.sender,
        factory=payload.factory,
        governance=payload.governance,
        starting_price=payload.starting_price,
        salt=payload.salt,
    )
    return {"status": status, "warnings": redact_sensitive_data(warnings), "data": redact_sensitive_data(data)}


@router.post("/auctions/{auction}/enable-tokens/prepare")
async def post_enable_tokens_prepare(
    auction: str,
    payload: AuctionEnableTokensPrepareRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    status, warnings, data = await prepare_enable_tokens_action(
        settings,
        session,
        operator_id=operator.operator_id,
        auction_address=auction,
        sender=payload.sender,
        extra_tokens=payload.extra_tokens,
    )
    return {"status": status, "warnings": redact_sensitive_data(warnings), "data": redact_sensitive_data(data)}


@router.post("/auctions/{auction}/settle/prepare")
async def post_settle_prepare(
    auction: str,
    payload: AuctionSettlePrepareRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    status, warnings, data = await prepare_settle_action(
        settings,
        session,
        operator_id=operator.operator_id,
        auction_address=auction,
        sender=payload.sender,
        token_address=payload.token_address,
        sweep=payload.sweep,
    )
    return {"status": status, "warnings": redact_sensitive_data(warnings), "data": redact_sensitive_data(data)}
