"""Durable operator-side queue for action lifecycle reports."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tidal.control_plane.client import ControlPlaneClient, ControlPlaneError
from tidal.time import utcnow_iso

_OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_report_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_url TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tx_index INTEGER NOT NULL,
    report_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(base_url, action_id, tx_index, report_type)
)
"""


def default_operator_state_dir() -> Path:
    override = os.getenv("TIDAL_OPERATOR_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tidal" / "operator-state"


def default_action_report_outbox_path() -> Path:
    return default_operator_state_dir() / "action_outbox.db"


@dataclass(frozen=True, slots=True)
class PendingActionReport:
    id: int
    action_id: str
    tx_index: int
    report_type: str
    payload: dict[str, Any]
    attempt_count: int
    last_error: str | None


class ActionReportOutbox:
    """Small SQLite-backed outbox for replayable action reports."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_action_report_outbox_path()
        self._conn: sqlite3.Connection | None = None

    def queue_broadcast(self, *, base_url: str, action_id: str, payload: dict[str, Any]) -> None:
        self._upsert(
            base_url=base_url,
            action_id=action_id,
            tx_index=int(payload["txIndex"]),
            report_type="broadcast",
            payload=payload,
        )

    def queue_receipt(self, *, base_url: str, action_id: str, payload: dict[str, Any]) -> None:
        self._upsert(
            base_url=base_url,
            action_id=action_id,
            tx_index=int(payload["txIndex"]),
            report_type="receipt",
            payload=payload,
        )

    def mark_delivered(
        self,
        *,
        base_url: str,
        action_id: str,
        tx_index: int,
        report_type: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM action_report_outbox
                WHERE base_url = ? AND action_id = ? AND tx_index = ? AND report_type = ?
                """,
                (self._normalize_base_url(base_url), action_id, tx_index, report_type),
            )

    def pending_reports(self, *, base_url: str, limit: int = 100) -> list[PendingActionReport]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, action_id, tx_index, report_type, payload_json, attempt_count, last_error
                FROM action_report_outbox
                WHERE base_url = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (self._normalize_base_url(base_url), limit),
            ).fetchall()

        pending: list[PendingActionReport] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                payload = {}
            pending.append(
                PendingActionReport(
                    id=int(row["id"]),
                    action_id=str(row["action_id"]),
                    tx_index=int(row["tx_index"]),
                    report_type=str(row["report_type"]),
                    payload=payload if isinstance(payload, dict) else {},
                    attempt_count=int(row["attempt_count"]),
                    last_error=str(row["last_error"]) if row["last_error"] is not None else None,
                )
            )
        return pending

    def pending_count(self, *, base_url: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM action_report_outbox WHERE base_url = ?",
                (self._normalize_base_url(base_url),),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def flush_pending(self, client: ControlPlaneClient, *, limit: int = 100) -> int:
        delivered = 0
        for report in self.pending_reports(base_url=client.base_url, limit=limit):
            try:
                if report.report_type == "broadcast":
                    client.report_broadcast(report.action_id, report.payload)
                else:
                    client.report_receipt(report.action_id, report.payload)
            except ControlPlaneError as exc:
                self._mark_failed(report.id, str(exc))
                if exc.status_code is None or exc.status_code >= 500:
                    break
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(report.id, str(exc))
                break
            else:
                self.mark_delivered(
                    base_url=client.base_url,
                    action_id=report.action_id,
                    tx_index=report.tx_index,
                    report_type=report.report_type,
                )
                delivered += 1
        return delivered

    def _upsert(
        self,
        *,
        base_url: str,
        action_id: str,
        tx_index: int,
        report_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO action_report_outbox (
                    base_url,
                    action_id,
                    tx_index,
                    report_type,
                    payload_json,
                    attempt_count,
                    last_error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)
                ON CONFLICT(base_url, action_id, tx_index, report_type)
                DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    last_error = NULL
                """,
                (
                    self._normalize_base_url(base_url),
                    action_id,
                    tx_index,
                    report_type,
                    json.dumps(payload, sort_keys=True),
                    now,
                    now,
                ),
            )

    def _mark_failed(self, report_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE action_report_outbox
                SET attempt_count = attempt_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, utcnow_iso(), report_id),
            )

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(_OUTBOX_SCHEMA)
        self._conn = conn
        return conn

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        return base_url.rstrip("/")
