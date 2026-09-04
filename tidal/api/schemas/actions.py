"""Action lifecycle request schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ActionBroadcastRequest(BaseModel):
    sender: str
    tx_hash: str = Field(alias="txHash")
    broadcast_at: str = Field(alias="broadcastAt")
    tx_index: int = Field(alias="txIndex", ge=0)

    model_config = {"populate_by_name": True}


class ActionReceiptRequest(BaseModel):
    """A reconciliation hint. Legacy client outcome fields are ignored."""

    tx_index: int = Field(alias="txIndex", ge=0)

    model_config = {"populate_by_name": True}
