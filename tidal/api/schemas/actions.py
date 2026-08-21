"""Action lifecycle request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ActionBroadcastRequest(BaseModel):
    sender: str
    tx_hash: str = Field(alias="txHash")
    broadcast_at: str = Field(alias="broadcastAt")
    tx_index: int = Field(alias="txIndex", ge=0)

    model_config = {"populate_by_name": True}


class ActionReceiptRequest(BaseModel):
    tx_index: int = Field(alias="txIndex", ge=0)
    receipt_status: Literal["CONFIRMED", "REVERTED", "FAILED"] = Field(
        alias="receiptStatus"
    )
    block_number: int | None = Field(default=None, alias="blockNumber")
    gas_used: int | None = Field(default=None, alias="gasUsed")
    gas_price_gwei: str | None = Field(default=None, alias="gasPriceGwei")
    observed_at: str = Field(alias="observedAt")
    error_message: str | None = Field(default=None, alias="errorMessage")

    model_config = {"populate_by_name": True}
