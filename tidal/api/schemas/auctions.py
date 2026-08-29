"""Auction prepare request schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuctionDeployPrepareRequest(BaseModel):
    want: str
    receiver: str
    sender: str | None = None
    factory: str | None = None
    governance: str | None = None
    starting_price: int = Field(
        alias="startingPrice",
        gt=0,
        description="Contract-raw startingPrice value; units depend on the approved factory version.",
    )
    salt: str | None = None

    model_config = {"populate_by_name": True}


class AuctionEnableTokensPrepareRequest(BaseModel):
    sender: str | None = None
    extra_tokens: list[str] = Field(default_factory=list, alias="extraTokens")
    txn_max_gas_limit: int | None = Field(default=None, alias="txnMaxGasLimit", ge=21_000)

    model_config = {"populate_by_name": True}


class AuctionSettlePrepareRequest(BaseModel):
    sender: str | None = None
    token_address: str | None = Field(default=None, alias="tokenAddress")
    force: bool = False

    model_config = {"populate_by_name": True}


class AuctionSweepPrepareRequest(BaseModel):
    sender: str | None = None
    token_address: str = Field(alias="tokenAddress")

    model_config = {"populate_by_name": True}
