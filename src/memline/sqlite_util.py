"""Shared SQLite plumbing for the small local stores (queue, pair store)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class SqliteStore:
    """One WAL-mode connection guarded by a process-local lock.

    Subclasses create their schema in ``__init__`` after calling super();
    rows come back as :class:`sqlite3.Row` so columns are accessed by name.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
