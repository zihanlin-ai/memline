"""Filesystem-backed RPC transport for namespace-isolated clients.

The normal daemon transport is a Unix socket.  Managed agent sandboxes can
see that socket in the shared workspace while the kernel refuses the connect
because the daemon lives in the host namespace.  Both sides can still access
the same workspace, so this module provides a small SQLite mailbox as a
transparent fallback.  It carries the exact same ``{"op", "args"}`` payloads
as the socket transport; execution remains in :mod:`memline.daemon` and
:mod:`memline.ops`.

SQLite is used instead of loose request files for atomic claiming, durable
responses, crash recovery, and concurrent clients.  The bridge never opens
Qdrant and never executes commands or paths supplied by a caller.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memline.config import STORE_DIR
from memline.sqlite_util import SqliteStore

BRIDGE_DB = STORE_DIR / "bridge.db"
PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.05
RETENTION_HOURS = 24


class BridgeUnavailable(RuntimeError):
    """Raised when no host daemon is servicing the shared mailbox."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class BridgeQueue(SqliteStore):
    """Persistent request/response mailbox shared by CLI and daemon."""

    def __init__(self, db_path: Any = None) -> None:
        path = Path(db_path or BRIDGE_DB)
        super().__init__(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS bridge_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    protocol_version INTEGER NOT NULL,
                    daemon_id TEXT NOT NULL,
                    daemon_pid INTEGER NOT NULL,
                    ready INTEGER NOT NULL,
                    heartbeat_at TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS bridge_requests (
                    id TEXT PRIMARY KEY,
                    protocol_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    response_json TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bridge_requests_status_created "
                "ON bridge_requests(status, created_at)"
            )
            self._conn.commit()

    # -- daemon lifecycle -------------------------------------------------

    def mark_ready(self, daemon_id: str, daemon_pid: int) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO bridge_state
                   (singleton, protocol_version, daemon_id, daemon_pid, ready, heartbeat_at)
                   VALUES (1, ?, ?, ?, 1, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     protocol_version=excluded.protocol_version,
                     daemon_id=excluded.daemon_id,
                     daemon_pid=excluded.daemon_pid,
                     ready=1,
                     heartbeat_at=excluded.heartbeat_at""",
                (PROTOCOL_VERSION, daemon_id, daemon_pid, now),
            )
            self._conn.commit()

    def heartbeat(self, daemon_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE bridge_state SET heartbeat_at = ? "
                "WHERE singleton = 1 AND daemon_id = ? AND ready = 1",
                (_now(), daemon_id),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def mark_stopped(self, daemon_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bridge_state SET ready = 0, heartbeat_at = ? "
                "WHERE singleton = 1 AND daemon_id = ?",
                (_now(), daemon_id),
            )
            self._conn.commit()

    def state(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bridge_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        heartbeat = _parse_time(row["heartbeat_at"])
        age = (
            (datetime.now(timezone.utc) - heartbeat).total_seconds()
            if heartbeat is not None
            else float("inf")
        )
        return {
            "protocol_version": row["protocol_version"],
            "daemon_id": row["daemon_id"],
            "daemon_pid": row["daemon_pid"],
            "ready": bool(row["ready"]),
            "heartbeat_at": row["heartbeat_at"],
            "heartbeat_age_seconds": age,
        }

    def is_ready(self) -> bool:
        state = self.state()
        return bool(
            state
            and state["protocol_version"] == PROTOCOL_VERSION
            and state["ready"]
            and state["heartbeat_age_seconds"] <= HEARTBEAT_STALE_SECONDS
        )

    # -- client side ------------------------------------------------------

    def enqueue(self, payload: dict[str, Any], request_id: str | None = None) -> str:
        request_id = request_id or uuid.uuid4().hex
        now = _now()
        encoded = json.dumps(payload, default=str)
        with self._lock:
            self._conn.execute(
                """INSERT INTO bridge_requests
                   (id, protocol_version, payload_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (request_id, PROTOCOL_VERSION, encoded, now, now),
            )
            self._conn.commit()
        return request_id

    def response(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, response_json FROM bridge_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        if row is None or row["status"] not in {"done", "error"}:
            return None
        payload = json.loads(row["response_json"] or "{}")
        payload["_bridge_status"] = row["status"]
        return payload

    def acknowledge(self, request_id: str) -> None:
        # Successful clients no longer need the durable response. Delete it
        # immediately so search text and other request payloads do not linger.
        with self._lock:
            self._conn.execute(
                "DELETE FROM bridge_requests WHERE id = ? AND status IN ('done', 'error')",
                (request_id,),
            )
            self._conn.commit()

    def cancel_if_queued(self, request_id: str, reason: str) -> bool:
        """Cancel work that timed out before a daemon claimed it.

        A processing request is deliberately left alone: like a timed-out
        socket request, its execution outcome is already ambiguous and must not
        be duplicated automatically.
        """
        response = {"status": "error", "error": reason}
        with self._lock:
            cur = self._conn.execute(
                "UPDATE bridge_requests SET status = 'error', response_json = ?, "
                "updated_at = ? WHERE id = ? AND status = 'queued'",
                (json.dumps(response), _now(), request_id),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    # -- daemon worker side ----------------------------------------------

    def claim_next(self) -> dict[str, Any] | None:
        with self._lock:
            # Avoid a write transaction for the overwhelmingly common empty
            # poll. The conditional UPDATE below is the atomic claim point.
            row = self._conn.execute(
                "SELECT id, payload_json FROM bridge_requests "
                "WHERE status = 'queued' AND protocol_version = ? "
                "ORDER BY created_at LIMIT 1",
                (PROTOCOL_VERSION,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE bridge_requests SET status = 'processing', updated_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (_now(), row["id"]),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if not cur.rowcount:
            return None
        return {"id": row["id"], "payload": json.loads(row["payload_json"])}

    def complete(self, request_id: str, response: dict[str, Any]) -> None:
        status = "done" if response.get("status") == "ok" else "error"
        with self._lock:
            self._conn.execute(
                "UPDATE bridge_requests SET status = ?, response_json = ?, updated_at = ? "
                "WHERE id = ?",
                (status, json.dumps(response, default=str), _now(), request_id),
            )
            self._conn.commit()

    def recover_stale(self) -> int:
        response = json.dumps(
            {
                "status": "error",
                "error": "host daemon restarted while bridge request was processing; "
                "execution outcome is unknown",
            }
        )
        with self._lock:
            cur = self._conn.execute(
                "UPDATE bridge_requests SET status = 'error', response_json = ?, updated_at = ? "
                "WHERE status = 'processing'",
                (response, _now()),
            )
            self._conn.commit()
        return cur.rowcount

    def purge(self, retention_hours: int = RETENTION_HOURS) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bridge_requests WHERE acknowledged = 1 OR "
                "(updated_at < ? AND status IN ('done', 'error'))",
                (cutoff,),
            )
            self._conn.commit()
        return cur.rowcount


def bridge_state(db_path: Any = None) -> dict[str, Any] | None:
    """Read bridge liveness without creating a database from a sandbox."""
    path = Path(db_path or BRIDGE_DB)
    if not path.exists():
        return None
    try:
        queue = BridgeQueue(path)
        return queue.state()
    except (OSError, sqlite3.Error):
        return None


def request(
    payload: dict[str, Any],
    *,
    timeout: float,
    db_path: Any = None,
) -> Any:
    """Submit one daemon payload through the shared mailbox and await it."""
    path = Path(db_path or BRIDGE_DB)
    if not path.exists():
        raise BridgeUnavailable("sandbox bridge database does not exist")
    try:
        queue = BridgeQueue(path)
        if not queue.is_ready():
            raise BridgeUnavailable("sandbox bridge has no fresh host-daemon heartbeat")
        request_id = queue.enqueue(payload)
    except (OSError, sqlite3.Error) as exc:
        raise BridgeUnavailable(f"sandbox bridge database is unavailable: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = queue.response(request_id)
        if response is not None:
            queue.acknowledge(request_id)
            response.pop("_bridge_status", None)
            if response.get("status") != "ok":
                raise RuntimeError(response.get("error") or "daemon bridge request failed")
            return response.get("result")
        if not queue.is_ready():
            cancelled = queue.cancel_if_queued(
                request_id, "host daemon stopped before bridge request was claimed"
            )
            if cancelled:
                raise BridgeUnavailable("host daemon stopped while bridge request was pending")
            # The request was already claimed. A graceful daemon shutdown
            # drains active workers, so continue waiting for that one response.
        time.sleep(POLL_INTERVAL_SECONDS)
    queue.cancel_if_queued(request_id, f"sandbox bridge request timed out after {timeout:.1f}s")
    raise BridgeUnavailable(f"sandbox bridge request timed out after {timeout:.1f}s")
