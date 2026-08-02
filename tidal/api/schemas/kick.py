"""Kick request schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class KickInspectRequest(BaseModel):
    source_type: str | None = Field(default=None, alias="sourceType")
    source_address: str | None = Field(default=None, alias="sourceAddress")
    auction_address: str | None = Field(default=None, alias="auctionAddress")
    token_address: str | None = Field(default=None, alias="tokenAddress")
    limit: int | None = Field(default=None, ge=1)
    min_usd_value: float | None = Field(default=None, alias="minUsdValue", ge=0)
    include_live_inspection: bool = Field(default=True, alias="includeLiveInspection")

    model_config = {"populate_by_name": True}


class KickPrepareRequest(KickInspectRequest):
    sender: str | None = None
    require_curve_quote: bool | None = Field(default=None, alias="requireCurveQuote")
    txn_max_gas_limit: int | None = Field(default=None, alias="txnMaxGasLimit", ge=21_000)
    allow_killed_gauge: bool = Field(default=False, alias="allowKilledGauge")
    allow_no_fill_retry: bool = Field(default=False, alias="allowNoFillRetry")

    @model_validator(mode="after")
    def validate_no_fill_override_scope(self) -> "KickPrepareRequest":
        if self.allow_no_fill_retry and not (self.auction_address and self.token_address):
            raise ValueError("allowNoFillRetry requires an exact auctionAddress and tokenAddress pair")
        return self

    model_config = {"populate_by_name": True}
