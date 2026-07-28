"""Persistent async-add event queue (official CLI ``event`` semantics).

Adds are acknowledged only after the event row is durably written, so a daemon
crash never loses an accepted memory: pending rows are re-claimed on startup.
Failed events stay queryable (``event list --status failed``) and retryable
(``event retry``); an alerts file lets the CLI surface unacknowledged failures
on any invocation without polling.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mem0_local.config import STORE_DIR
from mem0_local.sqlite_util import SqliteStore

QUEUE_DB = STORE_DIR / "queue.db"
ALERTS_FILE = STORE_DIR / "queue-alerts.json"
MAX_ATTEMPTS = 3
RETENTION_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventQueue(SqliteStore):
    def __init__(self, db_path: Any = None) -> None:
        super().__init__(Path(db_path or QUEUE_DB))
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    op TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    acked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                )"""
            )
            self._conn.commit()
        self.notify = threading.Condition()

    # -- intake -----------------------------------------------------------

    def enqueue(self, op: str, args: dict[str, Any]) -> str:
        event_id = uuid.uuid4().hex
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (id, op, args_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (event_id, op, json.dumps(args, default=str), now, now),
            )
            self._conn.commit()
        with self.notify:
            self.notify.notify()
        return event_id

    # -- worker side ------------------------------------------------------

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, op, args_json, attempts FROM events "
                "WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE events SET status = 'processing', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            self._conn.commit()
        return {
            "id": row["id"],
            "op": row["op"],
            "args": json.loads(row["args_json"]),
            "attempts": row["attempts"] + 1,
        }

    def complete(self, event_id: str, result: Any) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status = 'done', result_json = ?, error = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(result, default=str), _now(), event_id),
            )
            self._conn.commit()

    def fail(self, event_id: str, error: str, attempts: int) -> bool:
        """Mark a failure; requeue while attempts remain. Returns True if terminal."""
        terminal = attempts >= MAX_ATTEMPTS
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                ("failed" if terminal else "queued", error, _now(), event_id),
            )
            self._conn.commit()
        if not terminal:
            with self.notify:
                self.notify.notify()
        return terminal

    def purge(self, retention_days: int = RETENTION_DAYS) -> int:
        """Drop terminal rows older than the retention window.

        Only `done` and acknowledged `failed` rows are purged; unacked
        failures stay visible until someone retries or acks them. Every add
        is permanently recorded in the audit manifest, so purging queue rows
        loses nothing auditable.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE updated_at < ? AND "
                "(status = 'done' OR (status = 'failed' AND acked = 1))",
                (cutoff,),
            )
            self._conn.commit()
        return cur.rowcount

    def recover_stale(self) -> int:
        """Requeue rows stuck in 'processing' from a previous daemon crash."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET status = 'queued', updated_at = ? WHERE status = 'processing'",
                (_now(),),
            )
            self._conn.commit()
        return cur.rowcount

    # -- inspection / management ------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "event_id": row["id"],
            "op": row["op"],
            "status": row["status"],
            "attempts": row["attempts"],
            "acked": bool(row["acked"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
        }
        args = json.loads(row["args_json"])
        content = args.get("content")
        item["content_preview"] = str(content)[:120] if content is not None else None
        item["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        return item

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_args(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT args_json FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return json.loads(row["args_json"]) if row else None

    def list(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def retry(self, event_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET status = 'queued', attempts = 0, acked = 0, error = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'failed'",
                (_now(), event_id),
            )
            self._conn.commit()
        if cur.rowcount:
            with self.notify:
                self.notify.notify()
        return bool(cur.rowcount)

    def ack(self, event_id: str | None = None) -> int:
        """Acknowledge failed events (all when event_id is None)."""
        with self._lock:
            if event_id:
                cur = self._conn.execute(
                    "UPDATE events SET acked = 1, updated_at = ? WHERE id = ? AND status = 'failed'",
                    (_now(), event_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE events SET acked = 1, updated_at = ? WHERE status = 'failed' AND acked = 0",
                    (_now(),),
                )
            self._conn.commit()
        return cur.rowcount

    # -- alerts -------------------------------------------------------------

    def refresh_alerts(self) -> None:
        """Publish unacknowledged-failure state for the CLI banner."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MAX(updated_at) AS latest_at "
                "FROM events WHERE status = 'failed' AND acked = 0"
            ).fetchone()
            latest = self._conn.execute(
                "SELECT error FROM events WHERE status = 'failed' AND acked = 0 "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        count = row["n"] if row else 0
        if count:
            ALERTS_FILE.write_text(
                json.dumps(
                    {
                        "failed_unacked": count,
                        "latest_error": (latest["error"] if latest else None),
                        "updated_at": row["latest_at"],
                    }
                )
            )
        else:
            try:
                ALERTS_FILE.unlink()
            except FileNotFoundError:
                pass


def read_alerts() -> dict[str, Any] | None:
    """Cheap read of the alerts file (no DB open); None when no alerts."""
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
