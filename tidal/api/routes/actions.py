"""Action ledger routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from tidal.api.auth import OperatorIdentity
from tidal.api.dependencies import get_operator, get_session, get_settings
from tidal.api.errors import APIError
from tidal.api.schemas.actions import ActionBroadcastRequest, ActionReceiptRequest
from tidal.api.services.action_audit import get_action, list_actions, record_broadcast, record_receipt
from tidal.security import redact_sensitive_data
from tidal.config import Settings
from tidal.operation_reconciler import OperationReconciler
from tidal.runtime import build_web3_client

router = APIRouter()


@router.get("/actions")
def get_actions(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    operator: str | None = Query(default=None),
    status: str | None = Query(default=None),
    action_type: str | None = Query(default=None, alias="action_type"),
    session: Session = Depends(get_session),
    _current_operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    data = list_actions(session, limit=limit, offset=offset, operator_id=operator, status=status, action_type=action_type)
    return {
        "status": "ok" if data["items"] else "noop",
        "warnings": [],
        "data": redact_sensitive_data(data),
    }


@router.get("/actions/{action_id}")
def get_action_detail(
    action_id: str,
    session: Session = Depends(get_session),
    _operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    data = get_action(session, action_id)
    if data is None:
        raise APIError("Action not found", status_code=404)
    return {"status": "ok", "warnings": [], "data": redact_sensitive_data(data)}


@router.post("/actions/{action_id}/broadcast")
def post_action_broadcast(
    action_id: str,
    payload: ActionBroadcastRequest,
    session: Session = Depends(get_session),
    _operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    data = record_broadcast(
        session,
        action_id,
        tx_index=payload.tx_index,
        tx_hash=payload.tx_hash,
        broadcast_at=payload.broadcast_at,
    )
    return {"status": "ok", "warnings": [], "data": redact_sensitive_data(data)}


@router.post("/actions/{action_id}/receipt")
async def post_action_receipt(
    action_id: str,
    payload: ActionReceiptRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _operator: OperatorIdentity = Depends(get_operator),
) -> dict[str, object]:
    data = record_receipt(
        session,
        action_id,
        tx_index=payload.tx_index,
        receipt_status=payload.receipt_status,
        block_number=payload.block_number,
        gas_used=payload.gas_used,
        gas_price_gwei=payload.gas_price_gwei,
        observed_at=payload.observed_at,
        error_message=payload.error_message,
    )
    transactions = data.get("transactions")
    tx_hash = None
    if isinstance(transactions, list):
        for transaction in transactions:
            if isinstance(transaction, dict) and transaction.get("txIndex") == payload.tx_index:
                tx_hash = transaction.get("txHash")
                break
    if tx_hash and settings.rpc_url:
        web3_client = build_web3_client(settings)
        try:
            receipt = await web3_client.get_transaction_receipt(str(tx_hash), timeout_seconds=2)
            reconciler = OperationReconciler(
                session=session,
                web3_client=web3_client,
                auction_kicker_address=settings.auction_kicker_address,
            )
            await reconciler.finalize_receipt(str(tx_hash), receipt)
        except Exception:  # noqa: BLE001
            pass
        finally:
            await web3_client.close()
    return {"status": "ok", "warnings": [], "data": redact_sensitive_data(data)}
