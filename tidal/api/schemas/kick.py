"""Kick request schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class KickInspectRequest(BaseModel):
    source_type: str | None = Field(default=None, alias="sourceType")
    source_address: str | None = Field(default=None, alias="sourceAddress")
    auction_address: str | None = Field(default=None, alias="auctionAddress")
    token_address: str | None = Field(default=None, alias="tokenAddress")
    limit: int | None = Field(default=None, ge=1)
    include_live_inspection: bool = Field(default=True, alias="includeLiveInspection")

    model_config = {"populate_by_name": True}


class KickPrepareRequest(KickInspectRequest):
    sender: str | None = None
    require_curve_quote: bool | None = Field(default=None, alias="requireCurveQuote")

    model_config = {"populate_by_name": True}
