"""Bounded transition import of the old operator outbox into the main database.

Run while held after schema 0029 and before retiring the old action tables.
The caller preserves a coherent original DB/outbox pair. This module opens
sources read-only, makes no network calls, and never imports signed payloads.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from hexbytes import HexBytes
from sqlalchemy import insert, select, update

from tidal.legacy_operations import ensure_legacy_operations
from tidal.lifecycle import LifecycleError, clear_activation, execution_lock
from tidal.normalizers import normalize_address
from tidal.persistence import models
from tidal.transaction_evidence import rpc_int
from tidal.transactions import TransactionRepository, UNRESOLVED_STATUSES


def read_source(path: Path, table: str) -> list[dict]:
    """Only called with the fixed legacy table names below."""
    uri = f"file:{quote(str(path.expanduser().resolve()), safe='/')}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                raise LifecycleError("INVALID_LEGACY_SOURCE", "Legacy source failed its integrity check.")
            return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
    except sqlite3.Error as exc:
        raise LifecycleError("INVALID_LEGACY_SOURCE", "Legacy source is missing, unreadable or incompatible.") from exc


def _canonical(field: str, value):
    if value is None:
        return None
    if field in {"signer", "to_address"}:
        return normalize_address(str(value))
    if field in {"chain_id", "nonce"}:
        return rpc_int(value)
    if field == "value":
        return str(rpc_int(value))
    if field in {"tx_hash", "data"}:
        raw = bytes(HexBytes(value))
        if field == "tx_hash" and len(raw) != 32:
            raise ValueError("Invalid transaction hash")
        return "0x" + raw.hex()
    return value


def _merge(*sources: dict) -> dict:
    merged = {}
    for source in sources:
        for field in ("chain_id", "nonce", "signer", "tx_hash", "to_address", "data", "value"):
            value = _canonical(field, source.get(field))
            if value is None:
                continue
            if field in merged and merged[field] != value:
                raise LifecycleError("LEGACY_CONFLICT", f"Legacy records disagree on {field}; preserve originals and review.")
            merged[field] = value
    return merged


def import_legacy(*, settings, session, source_database: Path, outbox: Path) -> dict:
    """Import all report types, including delivered-but-retained submissions.

    An unverified receipt or delivery status cannot resolve an attempt. An
    already natively verified attempt remains resolved on repeated imports.
    """
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        clear_activation(settings.resolved_home_path / "activation.json")
        actions = {row["action_id"]: row for row in read_source(source_database, "api_actions")}
        prepared = {(row["action_id"], row["tx_index"]): row for row in read_source(source_database, "api_action_transactions")}
        reports = read_source(outbox, "action_report_outbox")
        source_transactions = {}
        for row in prepared.values():
            source_transactions.setdefault(row["action_id"], []).append(row)
        # Import useful intent for every retained API submission, including
        # actions whose reports were already removed from the old outbox.
        submissions = [
            {"report_type": "broadcast", "action_id": row["action_id"], "tx_index": row["tx_index"],
             "created_at": row.get("broadcast_at") or row["created_at"], "updated_at": row["updated_at"],
             "payload_json": json.dumps({"txHash": row["tx_hash"], "txIndex": row["tx_index"],
                                         "broadcastAt": row.get("broadcast_at")})}
            for row in prepared.values() if row.get("tx_hash")
        ]
        imported_ids: set[int] = set()
        try:
            for report in [*submissions, *reports]:
                if report["report_type"] not in {"submission", "broadcast", "receipt"}:
                    raise LifecycleError("INVALID_LEGACY_SOURCE", "Unrecognized legacy report type.")
                payload = json.loads(report["payload_json"])
                if not isinstance(payload, dict) or not payload.get("txHash"):
                    raise LifecycleError("INCOMPLETE_IDENTITY", "A retained report has no transaction hash; keep execution held for review.")
                if rpc_int(payload.get("txIndex", report["tx_index"])) != int(report["tx_index"]):
                    raise LifecycleError("LEGACY_CONFLICT", "Retained report transaction indexes disagree.")
                source = prepared.get((report["action_id"], report["tx_index"]), {})
                action = actions.get(report["action_id"], {})
                reported = {
                    "tx_hash": payload["txHash"], "signer": payload.get("sender"),
                    "chain_id": payload.get("chainId"), "nonce": payload.get("nonce"),
                }
                identity = _merge(source, {"signer": action.get("sender")}, reported)
                if identity.get("chain_id") not in (None, settings.chain_id):
                    raise LifecycleError("WRONG_CHAIN", "Retained submission belongs to another chain.")
                existing = session.execute(select(models.transactions).where(
                    models.transactions.c.tx_hash == identity["tx_hash"],
                )).mappings().all()
                if len(existing) > 1:
                    raise LifecycleError("LEGACY_CONFLICT", "Transaction hash has more than one retained identity.")
                row = dict(existing[0]) if existing else None
                if row is not None:
                    identity = _merge(row, identity)
                    transaction_id = int(row["id"])
                    # block_hash + verified_at is produced by native evidence
                    # validation; an old report's verified_at alone is weaker.
                    native_verified = row.get("block_hash") and row.get("verified_at") and row["status"] not in UNRESOLVED_STATUSES
                    session.execute(update(models.transactions).where(models.transactions.c.id == transaction_id).values(
                        **identity, **({} if native_verified else {"status": "PENDING"}),
                    ))
                else:
                    # Reuse a retained unsigned preview if this is its first
                    # reported identity. Otherwise keep a distinct attempt.
                    preview = session.execute(select(models.transactions).where(
                        models.transactions.c.action_id == report["action_id"],
                        models.transactions.c.tx_index == report["tx_index"],
                        models.transactions.c.tx_hash.is_(None),
                    )).mappings().first()
                    values = {
                        **identity, "status": "PENDING", "legacy": 1,
                        "operation": source.get("operation") or "legacy_unknown",
                        "created_at": str(payload.get("broadcastAt") or report["created_at"]),
                        "updated_at": str(report["updated_at"]),
                    }
                    if preview is not None:
                        transaction_id = int(preview["id"])
                        session.execute(update(models.transactions).where(models.transactions.c.id == transaction_id).values(**values))
                    else:
                        transaction_id = int(session.execute(insert(models.transactions).values(**values)).lastrowid)
                imported_ids.add(transaction_id)
                if source and action:
                    # Only add missing operation evidence. Never rewrite the
                    # original rows' times, amounts or baseline/round links.
                    ensure_legacy_operations(session, action_row=action, source_transactions=source_transactions[action["action_id"]], tx_row={
                        **source, "tx_hash": identity["tx_hash"],
                        "broadcast_at": payload.get("broadcastAt") or source.get("broadcast_at") or report["created_at"],
                    })
                session.execute(update(models.kick_txs).where(
                    models.kick_txs.c.tx_hash == identity["tx_hash"],
                    models.kick_txs.c.transaction_id.is_(None),
                ).values(transaction_id=transaction_id))
            session.commit()
        except BaseException as exc:
            session.rollback()
            if isinstance(exc, (ValueError, TypeError, KeyError)):
                raise LifecycleError("INVALID_LEGACY_SOURCE", "Malformed retained submission; keep originals and execution held.") from exc
            raise
        return {"reports_read": len(reports), "transactions_imported": len(imported_ids),
                "unresolved_attempts": len(TransactionRepository(session).unresolved()), "activated": False}
