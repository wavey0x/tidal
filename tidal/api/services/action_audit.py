"""Persistence and reconciliation for prepared operator actions."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tidal.api.errors import APIError
from tidal.config import Settings
from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.persistence.repositories import APIActionRepository, KickTxRepository
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso


def create_prepared_action(
    session: Session,
    *,
    operator_id: str,
    action_type: str,
    sender: str | None,
    request_payload: dict[str, Any],
    preview_payload: dict[str, Any],
    transactions: list[dict[str, Any]],
    resource_address: str | None = None,
    auction_address: str | None = None,
    source_address: str | None = None,
    token_address: str | None = None,
) -> str:
    now = utcnow_iso()
    action_id = str(uuid.uuid4())
    repo = APIActionRepository(session)
    repo.create(
        action_row={
            "action_id": action_id,
            "action_type": action_type,
            "status": "PREPARED",
            "operator_id": operator_id,
            "sender": sender,
            "resource_address": resource_address,
            "auction_address": auction_address,
            "source_address": source_address,
            "token_address": token_address,
            "request_json": json.dumps(request_payload),
            "preview_json": json.dumps(preview_payload),
            "created_at": now,
            "updated_at": now,
        },
        transaction_rows=[
            {
                "action_id": action_id,
                "tx_index": index,
                "operation": tx["operation"],
                "to_address": tx["to"],
                "data": tx["data"],
                "value": tx.get("value", "0x0"),
                "chain_id": tx["chainId"],
                "gas_estimate": tx.get("gasEstimate"),
                "gas_limit": tx.get("gasLimit"),
                "created_at": now,
                "updated_at": now,
            }
            for index, tx in enumerate(transactions)
        ],
    )
    return action_id


def list_actions(
    session: Session,
    *,
    limit: int,
    offset: int,
    operator_id: str | None = None,
    status: str | None = None,
    action_type: str | None = None,
) -> dict[str, object]:
    repo = APIActionRepository(session)
    count_stmt = select(func.count()).select_from(models.api_actions)
    if operator_id is not None:
        count_stmt = count_stmt.where(models.api_actions.c.operator_id == operator_id)
    if status is not None:
        count_stmt = count_stmt.where(models.api_actions.c.status == status)
    if action_type is not None:
        count_stmt = count_stmt.where(models.api_actions.c.action_type == action_type)
    total = int(session.execute(count_stmt).scalar_one())
    rows = repo.list_actions(
        limit=limit,
        offset=offset,
        operator_id=operator_id,
        status=status,
        action_type=action_type,
    )
    items = [_action_summary(row, repo.get_action_transactions(str(row["action_id"]))) for row in rows]
    return {"items": items, "total": total}


def get_action(session: Session, action_id: str) -> dict[str, object] | None:
    repo = APIActionRepository(session)
    row = repo.get_action(action_id)
    if row is None:
        return None
    transactions = repo.get_action_transactions(action_id)
    return _action_detail(row, transactions)


def record_broadcast(
    session: Session,
    action_id: str,
    *,
    tx_index: int,
    tx_hash: str,
    broadcast_at: str,
) -> dict[str, object]:
    repo = APIActionRepository(session)
    action_row, tx_row = _require_action_transaction(repo, action_id, tx_index=tx_index)

    current_tx_hash = str(tx_row["tx_hash"]) if tx_row.get("tx_hash") is not None else None
    if current_tx_hash is not None and current_tx_hash != tx_hash:
        raise APIError("Broadcast already recorded with a different tx hash", status_code=409)

    if current_tx_hash is None or tx_row.get("broadcast_at") is None:
        repo.update_transaction_broadcast(
            action_id,
            tx_index=tx_index,
            tx_hash=tx_hash,
            broadcast_at=broadcast_at,
        )
    transactions = repo.get_action_transactions(action_id)
    repo.update_action_status(action_id, status=_calculate_action_status(transactions), updated_at=broadcast_at)
    current_tx_row = _transaction_for_index(transactions, tx_index=tx_index)
    _sync_kick_log_rows(
        session,
        action_row=action_row,
        tx_row=current_tx_row,
        status="SUBMITTED",
        observed_at=broadcast_at,
    )
    row = repo.get_action(action_id)
    assert row is not None
    return _action_detail(row, transactions)


def record_receipt(
    session: Session,
    action_id: str,
    *,
    tx_index: int,
    receipt_status: str,
    block_number: int | None,
    gas_used: int | None,
    gas_price_gwei: str | None,
    observed_at: str,
    error_message: str | None = None,
) -> dict[str, object]:
    repo = APIActionRepository(session)
    action_row, tx_row = _require_action_transaction(repo, action_id, tx_index=tx_index)

    current_receipt_status = str(tx_row["receipt_status"]) if tx_row.get("receipt_status") is not None else None
    if current_receipt_status is not None and current_receipt_status != receipt_status:
        raise APIError("Receipt already recorded with a different status", status_code=409)
    if current_receipt_status is not None and _receipt_conflicts(
        tx_row,
        block_number=block_number,
        gas_used=gas_used,
        gas_price_gwei=gas_price_gwei,
        error_message=error_message,
    ):
        raise APIError("Receipt already recorded with different details", status_code=409)

    if current_receipt_status is None or _receipt_backfill_needed(
        tx_row,
        block_number=block_number,
        gas_used=gas_used,
        gas_price_gwei=gas_price_gwei,
        error_message=error_message,
    ):
        repo.update_transaction_receipt(
            action_id,
            tx_index=tx_index,
            receipt_status=receipt_status,
            block_number=block_number,
            gas_used=gas_used,
            gas_price_gwei=gas_price_gwei,
            observed_at=observed_at,
            error_message=error_message,
        )
    transactions = repo.get_action_transactions(action_id)
    repo.update_action_status(
        action_id,
        status=_calculate_action_status(transactions),
        updated_at=observed_at,
        error_message=error_message if receipt_status in {"FAILED", "REVERTED"} else None,
    )
    current_tx_row = _transaction_for_index(transactions, tx_index=tx_index)
    _sync_kick_log_rows(
        session,
        action_row=action_row,
        tx_row=current_tx_row,
        status=receipt_status,
        observed_at=observed_at,
        block_number=block_number,
        gas_used=gas_used,
        gas_price_gwei=gas_price_gwei,
        error_message=error_message,
    )
    row = repo.get_action(action_id)
    assert row is not None
    return _action_detail(row, repo.get_action_transactions(action_id))


async def run_receipt_reconciler(settings: Settings, database: Database) -> None:
    if not settings.rpc_url:
        return
    web3_client = build_web3_client(settings)
    interval_seconds = max(settings.tidal_api_receipt_reconcile_interval_seconds, 5)
    threshold_seconds = max(settings.tidal_api_receipt_reconcile_threshold_seconds, 0)
    try:
        while True:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)).isoformat()
            with database.session() as session:
                repo = APIActionRepository(session)
                pending_rows = repo.pending_receipt_transactions(older_than=cutoff)
                for row in pending_rows:
                    tx_hash = row.get("tx_hash")
                    if not tx_hash:
                        continue
                    try:
                        receipt = await web3_client.get_transaction_receipt(str(tx_hash), timeout_seconds=2)
                    except Exception:  # noqa: BLE001
                        continue
                    observed_at = utcnow_iso()
                    effective_gas_price = receipt.get("effectiveGasPrice")
                    gas_price_gwei = str(round(effective_gas_price / 1e9, 4)) if effective_gas_price else None
                    record_receipt(
                        session,
                        str(row["action_id"]),
                        tx_index=int(row["tx_index"]),
                        receipt_status="CONFIRMED" if receipt.get("status") == 1 else "REVERTED",
                        block_number=receipt.get("blockNumber"),
                        gas_used=receipt.get("gasUsed"),
                        gas_price_gwei=gas_price_gwei,
                        observed_at=observed_at,
                    )
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        return


def _calculate_action_status(transactions: list[dict[str, object]]) -> str:
    receipt_statuses = [row.get("receipt_status") for row in transactions]
    if any(status == "FAILED" for status in receipt_statuses):
        return "FAILED"
    if any(status == "REVERTED" for status in receipt_statuses):
        return "REVERTED"
    if transactions and all(status == "CONFIRMED" for status in receipt_statuses):
        return "CONFIRMED"
    if any(row.get("tx_hash") for row in transactions):
        return "BROADCAST_REPORTED"
    return "PREPARED"


def _sync_kick_log_rows(
    session: Session,
    *,
    action_row: dict[str, object],
    tx_row: dict[str, object],
    status: str,
    observed_at: str,
    block_number: int | None = None,
    gas_used: int | None = None,
    gas_price_gwei: str | None = None,
    error_message: str | None = None,
) -> None:
    if str(action_row.get("action_type") or "") != "kick":
        return
    if str(tx_row.get("operation") or "") != "kick":
        return

    tx_hash = tx_row.get("tx_hash")
    if tx_hash is None:
        return

    repo = KickTxRepository(session)
    run_id = f"api-action:{action_row['action_id']}"
    for operation in _prepared_kick_operations(action_row):
        existing = repo.find_by_run_and_identity(
            run_id=run_id,
            operation_type="kick",
            auction_address=operation["auction_address"],
            token_address=operation["token_address"],
        )
        if existing is None:
            row: dict[str, object] = {
                "run_id": run_id,
                "operation_type": "kick",
                "source_type": operation["source_type"],
                "source_address": operation["source_address"],
                "strategy_address": (
                    operation["source_address"] if operation["source_type"] == "strategy" else None
                ),
                "token_address": operation["token_address"],
                "auction_address": operation["auction_address"],
                "sell_amount": operation["sell_amount"],
                "starting_price": operation["starting_price"],
                "minimum_price": operation["minimum_price"],
                "usd_value": operation["usd_value"],
                "status": status,
                "tx_hash": str(tx_hash),
                "gas_used": gas_used,
                "gas_price_gwei": gas_price_gwei,
                "block_number": block_number,
                "error_message": error_message,
                "quote_amount": operation["quote_amount"],
                "quote_response_json": operation["quote_response_json"],
                "start_price_buffer_bps": operation["start_price_buffer_bps"],
                "min_price_buffer_bps": operation["min_price_buffer_bps"],
                "step_decay_rate_bps": operation["step_decay_rate_bps"],
                "settle_token": operation["settle_token"],
                "token_symbol": operation["token_symbol"],
                "want_address": operation["want_address"],
                "want_symbol": operation["want_symbol"],
                "normalized_balance": operation["sell_amount"],
                "created_at": str(tx_row.get("broadcast_at") or observed_at),
            }
            repo.insert(row)
            continue

        repo.update_status(
            int(existing["id"]),
            status=status,
            tx_hash=str(tx_hash),
            gas_used=gas_used,
            gas_price_gwei=gas_price_gwei,
            block_number=block_number,
            error_message=error_message,
        )


def _prepared_kick_operations(action_row: dict[str, object]) -> list[dict[str, object]]:
    preview = _decode_json(action_row.get("preview_json"))
    prepared = preview.get("preparedOperations")
    if not isinstance(prepared, list):
        return []

    items: list[dict[str, object]] = []
    for item in prepared:
        if not isinstance(item, dict) or item.get("operation") != "kick":
            continue
        auction_address = _optional_normalize_address(item.get("auctionAddress"))
        token_address = _optional_normalize_address(item.get("tokenAddress"))
        if auction_address is None or token_address is None:
            continue

        def _str(key: str) -> str | None:
            v = item.get(key)
            return str(v) if v is not None else None

        def _int(key: str) -> int | None:
            v = item.get(key)
            return int(v) if v is not None else None

        def _json_str(key: str) -> str | None:
            v = item.get(key)
            if v is None:
                return None
            if isinstance(v, str):
                return v
            try:
                return json.dumps(v, sort_keys=True)
            except (TypeError, ValueError):
                return None

        items.append(
            {
                "source_type": _str("sourceType"),
                "source_address": _optional_normalize_address(item.get("sourceAddress")),
                "auction_address": auction_address,
                "token_address": token_address,
                "token_symbol": _str("tokenSymbol"),
                "want_address": _optional_normalize_address(item.get("wantAddress")),
                "want_symbol": _str("wantSymbol"),
                "sell_amount": _str("sellAmount"),
                "starting_price": _str("startingPrice"),
                "minimum_price": _str("minimumPrice"),
                "usd_value": _str("usdValue"),
                "quote_amount": _str("quoteAmount"),
                "quote_response_json": _json_str("quoteResponseJson"),
                "start_price_buffer_bps": _int("bufferBps"),
                "min_price_buffer_bps": _int("minBufferBps"),
                "step_decay_rate_bps": _int("stepDecayRateBps"),
                "settle_token": _optional_normalize_address(item.get("settleToken")),
            }
        )
    return items


def _require_action_transaction(
    repo: APIActionRepository,
    action_id: str,
    *,
    tx_index: int,
) -> tuple[dict[str, object], dict[str, object]]:
    action_row = repo.get_action(action_id)
    if action_row is None:
        raise APIError("Action not found", status_code=404)
    tx_row = repo.get_action_transaction(action_id, tx_index=tx_index)
    if tx_row is None:
        raise APIError("Action transaction not found", status_code=404)
    return action_row, tx_row


def _transaction_for_index(transactions: list[dict[str, object]], *, tx_index: int) -> dict[str, object]:
    for row in transactions:
        if int(row["tx_index"]) == tx_index:
            return row
    raise APIError("Action transaction not found", status_code=404)


def _receipt_conflicts(
    tx_row: dict[str, object],
    *,
    block_number: int | None,
    gas_used: int | None,
    gas_price_gwei: str | None,
    error_message: str | None,
) -> bool:
    if block_number is not None and tx_row.get("block_number") is not None and int(tx_row["block_number"]) != block_number:
        return True
    if gas_used is not None and tx_row.get("gas_used") is not None and int(tx_row["gas_used"]) != gas_used:
        return True
    if gas_price_gwei is not None and tx_row.get("gas_price_gwei") is not None and str(tx_row["gas_price_gwei"]) != gas_price_gwei:
        return True
    if error_message is not None and tx_row.get("error_message") is not None and str(tx_row["error_message"]) != error_message:
        return True
    return False


def _receipt_backfill_needed(
    tx_row: dict[str, object],
    *,
    block_number: int | None,
    gas_used: int | None,
    gas_price_gwei: str | None,
    error_message: str | None,
) -> bool:
    return (
        (block_number is not None and tx_row.get("block_number") is None)
        or (gas_used is not None and tx_row.get("gas_used") is None)
        or (gas_price_gwei is not None and tx_row.get("gas_price_gwei") is None)
        or (error_message is not None and tx_row.get("error_message") is None)
    )


def _action_summary(action_row: dict[str, object], transactions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "actionId": action_row["action_id"],
        "actionType": action_row["action_type"],
        "status": action_row["status"],
        "operatorId": action_row["operator_id"],
        "sender": action_row["sender"],
        "auctionAddress": action_row["auction_address"],
        "sourceAddress": action_row["source_address"],
        "tokenAddress": action_row["token_address"],
        "createdAt": action_row["created_at"],
        "updatedAt": action_row["updated_at"],
        "transactionCount": len(transactions),
        "transactions": [_transaction_payload(row) for row in transactions],
    }


def _action_detail(action_row: dict[str, object], transactions: list[dict[str, object]]) -> dict[str, object]:
    return {
        **_action_summary(action_row, transactions),
        "resourceAddress": action_row["resource_address"],
        "request": _decode_json(action_row.get("request_json")),
        "preview": _decode_json(action_row.get("preview_json")),
        "errorMessage": action_row.get("error_message"),
    }


def _transaction_payload(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "txIndex": row["tx_index"],
        "operation": row["operation"],
        "to": row["to_address"],
        "data": row["data"],
        "value": row["value"],
        "chainId": row["chain_id"],
        "gasEstimate": row["gas_estimate"],
        "gasLimit": row["gas_limit"],
        "txHash": row["tx_hash"],
        "broadcastAt": row["broadcast_at"],
        "receiptStatus": row["receipt_status"],
        "blockNumber": row["block_number"],
        "gasUsed": row["gas_used"],
        "gasPriceGwei": row["gas_price_gwei"],
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _decode_json(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_normalize_address(value: object) -> str | None:
    if value is None:
        return None
    try:
        return normalize_address(str(value))
    except Exception:
        return None
