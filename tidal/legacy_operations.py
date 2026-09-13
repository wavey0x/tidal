"""Read retained legacy previews only during explicit state migration."""
from __future__ import annotations

import json
from typing import Any
from sqlalchemy.orm import Session
from tidal.normalizers import normalize_address
from tidal.persistence.repositories import KickTxRepository


def ensure_legacy_operations(
    session: Session,
    *,
    action_row: dict[str, object],
    tx_row: dict[str, object],
    source_transactions: list[dict],
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
    existing_operations = repo.list_by_tx_hash(str(tx_hash))
    operation_ids: set[int] = set()
    for operation in _prepared_log_operations(
        session,
        action_row,
        operation_type=operation_type,
        tx_index=int(tx_row["tx_index"]), source_transactions=source_transactions,
    ):
        existing = [
            row for row in existing_operations
            if row["run_id"] == run_id
            and _normalize_operation_type(row["operation_type"]) == operation_type
            and row["auction_address"] == operation["auction_address"]
            and row["token_address"] == operation["token_address"]
        ]
        if not existing:
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
                "created_at": str(tx_row.get("broadcast_at") or tx_row["created_at"]),
            }
            if operation_type in {"resolve_auction", "sweep_auction"}:
                round_kick = repo.latest_confirmed_unclosed_kick(
                    str(operation["auction_address"]),
                    str(operation["token_address"]),
                )
                if round_kick is not None:
                    row["round_kick_id"] = int(round_kick["id"])
            row_id = repo.insert(row, commit=False)
            existing_operations.append({**row, "id": row_id})
            operation_ids.add(row_id)
        else:
            for row in existing:
                row_id = int(row["id"])
                if row["operation_type"] != operation_type:
                    repo.update_fields(row_id, operation_type=operation_type)
                operation_ids.add(row_id)
    return operation_ids


def _prepared_log_operations(
    session: Session,
    action_row: dict[str, object],
    *,
    operation_type: str,
    tx_index: int,
    source_transactions: list[dict],
) -> list[dict[str, object]]:
    if str(action_row.get("action_type") or "") in {"kick", "settle", "sweep", "enable_tokens"}:
        return _prepared_preview_operations(
            session,
            action_row,
            operation_type=operation_type,
            tx_index=tx_index, source_transactions=source_transactions,
        )
    return []


def _prepared_preview_operations(
    session: Session,
    action_row: dict[str, object],
    *,
    operation_type: str,
    tx_index: int,
    source_transactions: list[dict],
) -> list[dict[str, object]]:
    preview = _decode_json(action_row.get("preview_json"))
    prepared = preview.get("preparedOperations")
    if "preparedOperations" not in preview and action_row.get("action_type") == "settle":
        # Pre-resolver settlement previews describe one transaction, not a batch.
        # Do not infer a tx-index association for multi-transaction actions.
        transactions = source_transactions
        inspection, decision = preview.get("inspection"), preview.get("decision")
        if (
            tx_index != 0 or len(transactions) != 1
            or operation_type not in {"resolve_auction", "sweep_auction"}
            or not isinstance(inspection, dict) or not isinstance(decision, dict)
        ):
            return []
        prepared = [{
            "operation": decision.get("operation_type") or decision.get("operationType"),
            "auctionAddress": action_row.get("auction_address") or inspection.get("auction_address"),
            "tokenAddress": action_row.get("token_address") or decision.get("token_address")
                or decision.get("tokenAddress") or inspection.get("active_token") or inspection.get("activeToken"),
            "sourceAddress": action_row.get("source_address"),
            "wantAddress": inspection.get("want_address") or inspection.get("wantAddress"),
            "reason": decision.get("reason"),
            "txIndex": 0,
        }]
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

    elif sum(_normalize_operation_type(row.get("operation")) == operation_type for row in source_transactions) != 1:
        # An index-free batch is only attributable if exactly one transaction
        # of this operation type exists in the retained original action.
        return []

    items: list[dict[str, object]] = []
    for item in matching_items:
        auction_address = _optional_normalize_address(item.get("auctionAddress"))
        token_address = _optional_normalize_address(item.get("tokenAddress"))
        if auction_address is None or token_address is None:
            continue
        source_context = {}  # Current discovery cannot establish historical source identity.

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
