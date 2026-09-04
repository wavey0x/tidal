"""Persistence and reconciliation for prepared operator actions."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tidal.api.errors import APIError
from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.persistence.repositories import APIActionRepository, KickTxRepository
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

    try:
        if current_tx_hash is None or tx_row.get("broadcast_at") is None:
            repo.update_transaction_broadcast(
                action_id,
                tx_index=tx_index,
                tx_hash=tx_hash,
                broadcast_at=broadcast_at,
            )
        transactions = repo.get_action_transactions(action_id)
        repo.update_action_status(
            action_id, status=_calculate_action_status(transactions), updated_at=utcnow_iso(), commit=False,
        )
        ensure_action_operations(
            session, action_row=action_row,
            tx_row=_transaction_for_index(transactions, tx_index=tx_index),
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    row = repo.get_action(action_id)
    assert row is not None
    return _action_detail(row, transactions)


def record_verified_receipt(
    session: Session,
    action_id: str,
    *,
    tx_index: int,
    receipt: dict[str, object],
    observed_at: str,
) -> None:
    """Stage chain-derived API state; the shared finalizer commits both ledgers."""
    repo = APIActionRepository(session)
    gas_price = receipt.get("effectiveGasPrice")
    repo.update_transaction_receipt(
        action_id,
        tx_index=tx_index,
        receipt_status="CONFIRMED" if int(receipt["status"]) == 1 else "REVERTED",
        block_number=int(receipt["blockNumber"]),
        gas_used=int(receipt["gasUsed"]) if receipt.get("gasUsed") is not None else None,
        gas_price_gwei=str(round(int(gas_price) / 1e9, 4)) if gas_price else None,
        observed_at=observed_at,
        verified_at=observed_at,
        error_message=None,
        commit=False,
    )
    repo.update_action_status(
        action_id,
        status=_calculate_action_status(repo.get_action_transactions(action_id)),
        updated_at=observed_at,
        error_message=None,
        commit=False,
    )


def _calculate_action_status(transactions: list[dict[str, object]]) -> str:
    receipt_statuses = [row.get("receipt_status") if row.get("verified_at") else None for row in transactions]
    if any(status == "FAILED" for status in receipt_statuses):
        return "FAILED"
    if any(status == "REVERTED" for status in receipt_statuses):
        return "REVERTED"
    if transactions and all(status == "CONFIRMED" for status in receipt_statuses):
        return "CONFIRMED"
    if any(row.get("tx_hash") for row in transactions):
        return "BROADCAST_REPORTED"
    return "PREPARED"


def ensure_action_operations(
    session: Session,
    *,
    action_row: dict[str, object],
    tx_row: dict[str, object],
) -> set[int]:
    """Stage missing operation rows for this exact transaction; never downgrade existing rows."""
    operation_type = _normalize_operation_type(tx_row.get("operation"))
    if operation_type not in {"kick", "resolve_auction", "sweep_auction", "enable_tokens"}:
        return set()

    tx_hash = tx_row.get("tx_hash")
    if tx_hash is None:
        return set()

    repo = KickTxRepository(session)
    run_id = f"api-action:{action_row['action_id']}"
    operation_ids: set[int] = set()
    for operation in _prepared_log_operations(
        session,
        action_row,
        operation_type=operation_type,
        tx_index=int(tx_row["tx_index"]),
    ):
        existing = repo.find_by_run_and_identity(
            run_id=run_id,
            operation_type=operation_type,
            auction_address=operation["auction_address"],
            token_address=operation["token_address"],
            tx_hash=str(tx_hash),
        )
        if existing is None:
            row: dict[str, object] = {
                "run_id": run_id,
                "operation_type": operation_type,
                "source_type": operation["source_type"],
                "source_address": operation["source_address"],
                "strategy_address": (
                    operation["source_address"] if operation["source_type"] == "strategy" else None
                ),
                "token_address": operation["token_address"],
                "auction_address": operation["auction_address"],
                "sell_amount": None,
                "requested_sell_amount": (
                    operation["sell_amount"] if operation_type == "kick" else None
                ),
                "starting_price": operation["starting_price"],
                "minimum_price": operation["minimum_price"],
                "minimum_quote": operation["minimum_quote"],
                "usd_value": operation["usd_value"],
                "status": "SUBMITTED",
                "tx_hash": str(tx_hash),
                "quote_amount": operation["quote_amount"],
                "quote_response_json": operation["quote_response_json"],
                "start_price_buffer_bps": operation["start_price_buffer_bps"],
                "min_price_buffer_bps": operation["min_price_buffer_bps"],
                "step_decay_rate_bps": operation["step_decay_rate_bps"],
                "settle_token": operation["settle_token"],
                "stuck_abort_reason": operation["stuck_abort_reason"],
                "token_symbol": operation["token_symbol"],
                "want_address": operation["want_address"],
                "want_symbol": operation["want_symbol"],
                "normalized_balance": None,
                "created_at": str(tx_row.get("broadcast_at") or utcnow_iso()),
            }
            if operation_type in {"resolve_auction", "sweep_auction"}:
                round_kick = repo.latest_confirmed_unclosed_kick(
                    str(operation["auction_address"]),
                    str(operation["token_address"]),
                )
                if round_kick is not None:
                    row["round_kick_id"] = int(round_kick["id"])
            operation_ids.add(repo.insert(row, commit=False))
        else:
            operation_ids.add(int(existing["id"]))
    return operation_ids


def _prepared_log_operations(
    session: Session,
    action_row: dict[str, object],
    *,
    operation_type: str,
    tx_index: int,
) -> list[dict[str, object]]:
    if str(action_row.get("action_type") or "") in {"kick", "settle", "sweep", "enable_tokens"}:
        return _prepared_preview_operations(
            session,
            action_row,
            operation_type=operation_type,
            tx_index=tx_index,
        )
    return []


def _prepared_preview_operations(
    session: Session,
    action_row: dict[str, object],
    *,
    operation_type: str,
    tx_index: int,
) -> list[dict[str, object]]:
    preview = _decode_json(action_row.get("preview_json"))
    prepared = preview.get("preparedOperations")
    if not isinstance(prepared, list):
        return []

    matching_items = [
        item
        for item in prepared
        if isinstance(item, dict) and _normalize_operation_type(item.get("operation")) == operation_type
    ]
    if any(_valid_tx_index(item.get("txIndex")) is not None for item in matching_items):
        matching_items = [
            item
            for item in matching_items
            if _valid_tx_index(item.get("txIndex")) == tx_index
        ]

    items: list[dict[str, object]] = []
    for item in matching_items:
        auction_address = _optional_normalize_address(item.get("auctionAddress"))
        token_address = _optional_normalize_address(item.get("tokenAddress"))
        if auction_address is None or token_address is None:
            continue
        source_context = _resolve_source_context(session, auction_address)

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
                "source_type": _str("sourceType") or source_context.get("source_type"),
                "source_address": _optional_normalize_address(item.get("sourceAddress")) or source_context.get("source_address"),
                "auction_address": auction_address,
                "token_address": token_address,
                "token_symbol": _str("tokenSymbol"),
                "want_address": _optional_normalize_address(item.get("wantAddress")) or source_context.get("want_address"),
                "want_symbol": _str("wantSymbol"),
                "sell_amount": _str("sellAmount"),
                "normalized_balance": _str("normalizedBalance") or _str("sellAmount"),
                "starting_price": _str("startingPrice"),
                "minimum_price": _str("minimumPriceScaled1e18") or _str("minimumPrice"),
                "minimum_quote": _str("minimumQuote"),
                "usd_value": _str("usdValue"),
                "quote_amount": _str("quoteAmount"),
                "quote_response_json": _json_str("quoteResponseJson"),
                "start_price_buffer_bps": _int("bufferBps"),
                "min_price_buffer_bps": _int("minBufferBps"),
                "step_decay_rate_bps": _int("stepDecayRateBps"),
                "settle_token": None,
                "stuck_abort_reason": _str("reason"),
            }
        )
    return items


def _resolve_source_context(session: Session, auction_address: str) -> dict[str, str]:
    strategy_row = session.execute(
        select(models.strategies.c.address, models.strategies.c.want_address).where(
            models.strategies.c.auction_address == auction_address
        )
    ).mappings().first()
    if strategy_row is not None:
        return {
            "source_type": "strategy",
            "source_address": normalize_address(str(strategy_row["address"])),
            "want_address": normalize_address(str(strategy_row["want_address"])) if strategy_row["want_address"] else None,
        }

    fee_burner_row = session.execute(
        select(models.fee_burners.c.address, models.fee_burners.c.want_address).where(
            models.fee_burners.c.auction_address == auction_address
        )
    ).mappings().first()
    if fee_burner_row is not None:
        return {
            "source_type": "fee_burner",
            "source_address": normalize_address(str(fee_burner_row["address"])),
            "want_address": normalize_address(str(fee_burner_row["want_address"])) if fee_burner_row["want_address"] else None,
        }

    return {}


def _normalize_operation_type(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().replace("-", "_")
    if normalized == "settle":
        return "resolve_auction"
    if normalized in {"sweep", "sweep_and_settle"}:
        return "sweep_auction"
    return normalized or None


def _valid_tx_index(value: object) -> int | None:
    if value is None:
        return None
    try:
        tx_index = int(value)
    except (TypeError, ValueError):
        return None
    return tx_index if tx_index >= 0 else None


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


def _action_summary(action_row: dict[str, object], transactions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "actionId": action_row["action_id"],
        "actionType": action_row["action_type"],
        "status": _calculate_action_status(transactions),
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
        "errorMessage": action_row.get("error_message") if all(row.get("verified_at") for row in transactions) else None,
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
        "receiptStatus": row["receipt_status"] if row.get("verified_at") else None,
        "verifiedAt": row.get("verified_at"),
        "blockNumber": row["block_number"] if row.get("verified_at") else None,
        "gasUsed": row["gas_used"] if row.get("verified_at") else None,
        "gasPriceGwei": row["gas_price_gwei"] if row.get("verified_at") else None,
        "errorMessage": row["error_message"] if row.get("verified_at") else None,
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
