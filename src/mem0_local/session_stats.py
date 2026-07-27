"""Per-session add counters behind the handoff-pressure banner.

Every live ``add`` audit row increments its writer session's counter (both the
synchronous CLI path and the daemon's async extraction path funnel through
``append_live_audit``). The CLI callback reads the current session's count and,
past the configured threshold, prints an advisory banner suggesting a handoff.
"""

from __future__ import annotations

import threading
from pathlib import Path

from mem0_local.config import SESSION_STATS_DB
from mem0_local.sqlite_util import SqliteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_add_counts (
    session_id   TEXT PRIMARY KEY,
    add_count    INTEGER NOT NULL DEFAULT 0,
    first_add_at TEXT,
    last_add_at  TEXT
);
"""


class SessionStatsStore(SqliteStore):
    """Monotonic per-session add counters, one row per writer session."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def record_add(self, session_id: str, at_iso: str) -> int:
        """Increment the session's counter and return the new count."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO session_add_counts (session_id, add_count, first_add_at, last_add_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    add_count = add_count + 1,
                    last_add_at = excluded.last_add_at
                """,
                (session_id, at_iso, at_iso),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT add_count FROM session_add_counts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["add_count"]) if row else 0

    def add_count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT add_count FROM session_add_counts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["add_count"]) if row else 0


_store_lock = threading.Lock()
_store: SessionStatsStore | None = None


def session_stats_store(path: Path | None = None) -> SessionStatsStore:
    """Process-wide stats store on the default path; fresh instance otherwise."""
    global _store
    if path is not None:
        return SessionStatsStore(path)
    with _store_lock:
        if _store is None:
            _store = SessionStatsStore(SESSION_STATS_DB)
        return _store
