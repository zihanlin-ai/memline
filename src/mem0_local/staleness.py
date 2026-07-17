"""Supersession (staleness) semantics for the local memory store.

Implements the invalidation data model from
``.agents/skills/local-memory/references/staleness-design.md``:

- Memory state lives in payload fields: ``superseded_by`` (list of memory
  ids; absent/empty = active), ``superseded_at``, ``superseded_reason``,
  ``stale_check_pin``. Invalidation never touches the memory text or its
  dense/BM25 vectors (payload-only ``set_payload``), and is reversible.
- Suspicion pairs are append-only evidence rows in a local SQLite table,
  keyed ``(new_id, old_id, old_text_hash)`` so judgments expire
  automatically when the old entry's text changes.
- The supersession relation is a DAG stored as per-memory adjacency lists;
  ``invalidate`` refuses cycles, ``resolve_head`` walks to the active heads.

All functions here take the vendored ``mem0.Memory`` client (or its
sub-objects) as an argument; nothing is process-global except the pair
store path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mem0_local.config import STORE_DIR

SUPERSEDED_BY = "superseded_by"
SUPERSEDED_AT = "superseded_at"
SUPERSEDED_REASON = "superseded_reason"
STALE_PIN = "stale_check_pin"

MAX_CHAIN_DEPTH = 100
# Verdicts below this confidence are cached (never re-judged) but do not
# open a suspicion for review.
SUSPICION_CONFIDENCE_FLOOR = 0.6

STALE_DB = STORE_DIR / "stale.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def superseded_ids(payload_or_meta: dict[str, Any] | None) -> list[str]:
    """Return the successor ids recorded on a payload or result metadata."""
    if not payload_or_meta:
        return []
    value = payload_or_meta.get(SUPERSEDED_BY)
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def is_invalidated(payload_or_meta: dict[str, Any] | None) -> bool:
    return bool(superseded_ids(payload_or_meta))


def result_item_superseded(item: dict[str, Any]) -> list[str]:
    """Successor ids for a search/list/get result item.

    mem0 promotes unknown payload keys into the item's ``metadata`` dict, but
    be tolerant of both layouts.
    """
    return superseded_ids(item.get("metadata") or {}) or superseded_ids(item)


def _point_payload(client: Any, memory_id: str) -> dict[str, Any] | None:
    point = client.vector_store.get(memory_id)
    if point is None:
        return None
    payload = getattr(point, "payload", None)
    return dict(payload) if payload else {}


# ---------------------------------------------------------------------------
# Invalidate / revive / resolve-head
# ---------------------------------------------------------------------------


class StalenessError(ValueError):
    """Raised for invalid supersession operations (missing ids, cycles...)."""


def _assert_no_cycle(client: Any, target_id: str, by_ids: list[str]) -> None:
    """Refuse an edge that would make ``target_id`` an ancestor of itself.

    Walks successor pointers starting from ``by_ids``; reaching ``target_id``
    means the proposed superseders are (transitively) already superseded by
    the target.
    """
    frontier = list(by_ids)
    visited: set[str] = set()
    depth = 0
    while frontier and depth < MAX_CHAIN_DEPTH:
        next_frontier: list[str] = []
        for node in frontier:
            if node == target_id:
                raise StalenessError(
                    f"invalidate would create a supersession cycle via {node}"
                )
            if node in visited:
                continue
            visited.add(node)
            payload = _point_payload(client, node)
            if payload:
                next_frontier.extend(superseded_ids(payload))
        frontier = next_frontier
        depth += 1
    if frontier:
        raise StalenessError(
            f"supersession chain exceeds {MAX_CHAIN_DEPTH} hops; refusing"
        )


def invalidate(
    client: Any,
    memory_id: str,
    by_ids: list[str],
    *,
    reason: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark ``memory_id`` as superseded by ``by_ids``.

    Payload-only mutation: text, embeddings, created_at are untouched.
    Reversible via :func:`revive`. Also closes any open suspicion pairs
    targeting the memory (they are moot once it leaves the pool).
    """
    by_ids = [b for b in dict.fromkeys(by_ids) if b]
    if not by_ids:
        raise StalenessError("invalidate requires at least one superseding id")
    if memory_id in by_ids:
        raise StalenessError("a memory cannot supersede itself")

    payload = _point_payload(client, memory_id)
    if payload is None:
        raise StalenessError(f"memory not found: {memory_id}")
    existing = superseded_ids(payload)
    if existing:
        raise StalenessError(
            f"memory {memory_id} is already invalidated (superseded_by={existing}); "
            "revive it first if this is a correction"
        )
    for by_id in by_ids:
        if _point_payload(client, by_id) is None:
            raise StalenessError(f"superseding memory not found: {by_id}")
    _assert_no_cycle(client, memory_id, by_ids)

    now = _now_iso()
    patch: dict[str, Any] = {
        SUPERSEDED_BY: by_ids,
        SUPERSEDED_AT: now,
        SUPERSEDED_REASON: reason,
        "invalidated_by_agent_id": actor_id,
        "invalidated_session_id": session_id,
    }
    client.vector_store.update(vector_id=memory_id, vector=None, payload=patch)

    text = str(payload.get("data") or "")
    try:
        client.db.add_history(
            memory_id,
            text,
            text,
            "INVALIDATE",
            updated_at=now,
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 - history is best-effort, audit is authoritative.
        pass

    closed = pair_store().close_for_old(memory_id, "obsoleted", disposed_by=actor_id)
    return {
        "id": memory_id,
        "invalidated": True,
        SUPERSEDED_BY: by_ids,
        SUPERSEDED_AT: now,
        SUPERSEDED_REASON: reason,
        "closed_open_suspicions": closed,
    }


def revive(
    client: Any,
    memory_id: str,
    *,
    actor_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Clear supersession state so ``memory_id`` re-enters the default pool."""
    payload = _point_payload(client, memory_id)
    if payload is None:
        raise StalenessError(f"memory not found: {memory_id}")
    previous = superseded_ids(payload)
    if not previous:
        raise StalenessError(f"memory {memory_id} is not invalidated")

    now = _now_iso()
    patch: dict[str, Any] = {
        SUPERSEDED_BY: None,
        SUPERSEDED_AT: None,
        SUPERSEDED_REASON: None,
        "revived_at": now,
        "revived_by_agent_id": actor_id,
        "revived_session_id": session_id,
    }
    client.vector_store.update(vector_id=memory_id, vector=None, payload=patch)

    text = str(payload.get("data") or "")
    try:
        client.db.add_history(
            memory_id,
            text,
            text,
            "REVIVE",
            updated_at=now,
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "id": memory_id,
        "revived": True,
        "previous_superseded_by": previous,
        "revived_at": now,
    }


def resolve_head(
    get_payload: Callable[[str], dict[str, Any] | None],
    memory_id: str,
) -> dict[str, Any]:
    """Follow supersession pointers from ``memory_id`` to the active head(s).

    ``get_payload`` maps a memory id to its payload dict (or None). Returns
    the requested id, the set of active heads, and the hop count. A split can
    legitimately produce more than one head.
    """
    start = get_payload(memory_id)
    if start is None:
        raise StalenessError(f"memory not found: {memory_id}")

    heads: list[str] = []
    visited: set[str] = set()
    frontier = [memory_id]
    hops = 0
    while frontier and hops <= MAX_CHAIN_DEPTH:
        next_frontier: list[str] = []
        for node in frontier:
            if node in visited:
                continue
            visited.add(node)
            payload = get_payload(node) or {}
            successors = superseded_ids(payload)
            if successors:
                next_frontier.extend(successors)
            elif payload:
                heads.append(node)
        frontier = next_frontier
        if frontier:
            hops += 1
    return {"requested": memory_id, "heads": heads, "hops": hops}


# ---------------------------------------------------------------------------
# Search integration
# ---------------------------------------------------------------------------


def search_with_staleness(
    client: Any,
    *,
    query: str,
    top_k: int,
    filters: dict[str, Any] | None,
    threshold: float,
    rerank: bool,
    keyword: bool,
    explain: bool,
    include_superseded: bool = False,
) -> Any:
    """Default search: over-fetch, drop invalidated, flag suspected.

    With ``include_superseded`` the raw result is returned unchanged (full
    history digs). Otherwise invalidated entries are filtered out (over-fetch
    keeps top_k honest) and any hit with open suspicion pairs is annotated
    with ``suspected_stale`` so stale candidates are flagged, not silent.
    """
    fetch_k = top_k if include_superseded else max(top_k * 2, top_k + 10)
    result = client.search(
        query,
        top_k=fetch_k,
        filters=filters,
        threshold=threshold,
        rerank=rerank,
        keyword=keyword,
        explain=explain,
    )
    if include_superseded:
        return result

    items = result.get("results") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return result

    kept = [i for i in items if not result_item_superseded(i)][:top_k]
    annotate_suspected(kept)
    result["results"] = kept
    return result


def annotate_suspected(items: list[dict[str, Any]]) -> None:
    """Attach open-suspicion info to result items in place."""
    ids = [str(i.get("id")) for i in items if i.get("id")]
    if not ids:
        return
    try:
        open_by_old = pair_store().open_for_old_ids(ids)
    except Exception:  # noqa: BLE001 - annotation must never break search.
        return
    if not open_by_old:
        return
    for item in items:
        pairs = open_by_old.get(str(item.get("id")))
        if pairs:
            item["suspected_stale"] = True
            item["suspicions"] = [
                {
                    "suspected_by": p["new_id"],
                    "verdict": p["verdict"],
                    "confidence": p["confidence"],
                    "reason": p["reason"],
                }
                for p in pairs
            ]


# ---------------------------------------------------------------------------
# Background stale-check (queue worker entry point)
# ---------------------------------------------------------------------------

STALE_CHECK_TOP_K = 10


def run_stale_check(
    client: Any,
    new_id: str,
    *,
    session_id: str | None = None,
    top_k: int = STALE_CHECK_TOP_K,
    llm: Any = None,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """Judge one new entry against its top-k active neighbors (advisory only).

    Produces suspicion-pair evidence rows; never changes memory state. Safe
    to re-run: the pair cache skips already-judged (old_id, text-version)
    combinations.
    """
    payload = _point_payload(client, new_id)
    if payload is None:
        return {"new_id": new_id, "skipped": "memory no longer exists"}
    if is_invalidated(payload):
        return {"new_id": new_id, "skipped": "memory already invalidated"}
    new_text = str(payload.get("data") or "")
    if not new_text:
        return {"new_id": new_id, "skipped": "empty text"}

    filters = {"user_id": payload["user_id"]} if payload.get("user_id") else None
    raw = client.search(
        new_text,
        top_k=max(top_k * 2, top_k + 10),
        filters=filters,
        threshold=0.1,
        rerank=False,
        keyword=False,
        explain=False,
    )
    items = raw.get("results") if isinstance(raw, dict) else []
    candidates: list[dict[str, Any]] = []
    for item in items or []:
        cand_id = str(item.get("id") or "")
        meta = item.get("metadata") or {}
        if (
            not cand_id
            or cand_id == new_id
            or result_item_superseded(item)
            or meta.get(STALE_PIN)
            or item.get(STALE_PIN)
        ):
            continue
        candidates.append(
            {
                "id": cand_id,
                "text": str(item.get("memory") or ""),
                "date": str(item.get("created_at") or "")[:10],
            }
        )
        if len(candidates) >= top_k:
            break

    store = pair_store()
    already = store.judged_pairs(new_id, [(c["id"], c["text"]) for c in candidates])
    candidates = [c for c in candidates if c["id"] not in already]
    if not candidates:
        return {"new_id": new_id, "judged": 0, "opened": 0, "cached": len(already)}

    from mem0_local.judge import judge as judge_fn

    llm = llm or client.llm
    new_entry = {
        "id": new_id,
        "text": new_text,
        "date": str(payload.get("created_at") or "")[:10],
    }
    judgments = judge_fn(llm, new_entry, candidates)

    text_by_id = {c["id"]: c["text"] for c in candidates}
    opened = 0
    for verdict in judgments:
        row = store.record_judgment(
            new_id=new_id,
            old_id=verdict["id"],
            old_text=text_by_id.get(verdict["id"], ""),
            verdict=verdict["verdict"],
            confidence=verdict["confidence"],
            reason=verdict["reason"],
            judge_model=judge_model,
            new_session_id=session_id,
        )
        if row["disposition"] == "open" and row["inserted"]:
            opened += 1
    return {
        "new_id": new_id,
        "judged": len(judgments),
        "opened": opened,
        "cached": len(already),
    }


# ---------------------------------------------------------------------------
# Suspicion-pair evidence store
# ---------------------------------------------------------------------------

_pair_store_lock = threading.Lock()
_pair_store: "PairStore | None" = None


def pair_store(path: Path | None = None) -> "PairStore":
    """Process-wide pair store on the default path; fresh instance otherwise."""
    global _pair_store
    if path is not None:
        return PairStore(path)
    with _pair_store_lock:
        if _pair_store is None:
            _pair_store = PairStore(STALE_DB)
        return _pair_store


class PairStore:
    """Append-only suspicion-pair evidence rows (design §2.2).

    A pair is uniquely identified by ``(new_id, old_id, old_text_hash)`` —
    judgments are scoped to a specific version of the old text and expire
    automatically when it changes. Rows are never deleted; dispositions move
    ``open`` pairs to ``confirmed``/``dismissed``/``obsoleted``.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS pairs (
                    pair_id       TEXT PRIMARY KEY,
                    new_id        TEXT NOT NULL,
                    old_id        TEXT NOT NULL,
                    old_text_hash TEXT NOT NULL,
                    verdict       TEXT NOT NULL,
                    confidence    REAL,
                    reason        TEXT,
                    judged_at     TEXT NOT NULL,
                    judge_model   TEXT,
                    new_session_id TEXT,
                    disposition   TEXT NOT NULL DEFAULT 'open',
                    disposed_by   TEXT,
                    disposed_at   TEXT,
                    UNIQUE(new_id, old_id, old_text_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_pairs_old ON pairs(old_id, disposition);
                CREATE INDEX IF NOT EXISTS idx_pairs_new ON pairs(new_id, disposition);
                CREATE INDEX IF NOT EXISTS idx_pairs_session ON pairs(new_session_id, disposition);
                """
            )

    def record_judgment(
        self,
        *,
        new_id: str,
        old_id: str,
        old_text: str,
        verdict: str,
        confidence: float | None,
        reason: str | None,
        judge_model: str | None = None,
        new_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert one judgment; no-op if this exact pair version was judged."""
        opens = (
            verdict in {"SUPERSEDED", "DUPLICATE"}
            and (confidence or 0.0) >= SUSPICION_CONFIDENCE_FLOOR
        )
        row = {
            "pair_id": str(uuid.uuid4()),
            "new_id": new_id,
            "old_id": old_id,
            "old_text_hash": text_hash(old_text),
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "judged_at": _now_iso(),
            "judge_model": judge_model,
            "new_session_id": new_session_id,
            "disposition": "open" if opens else "cached",
        }
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO pairs (
                    pair_id, new_id, old_id, old_text_hash, verdict, confidence,
                    reason, judged_at, judge_model, new_session_id, disposition
                ) VALUES (
                    :pair_id, :new_id, :old_id, :old_text_hash, :verdict,
                    :confidence, :reason, :judged_at, :judge_model,
                    :new_session_id, :disposition
                )
                """,
                row,
            )
            self._conn.commit()
        row["inserted"] = cursor.rowcount > 0
        return row

    def judged_pairs(self, new_id: str, candidates: list[tuple[str, str]]) -> set[str]:
        """Return old_ids already judged for (new_id, old_id, hash(old_text))."""
        judged: set[str] = set()
        with self._lock:
            for old_id, old_text in candidates:
                cur = self._conn.execute(
                    "SELECT 1 FROM pairs WHERE new_id=? AND old_id=? AND old_text_hash=?",
                    (new_id, old_id, text_hash(old_text)),
                )
                if cur.fetchone():
                    judged.add(old_id)
        return judged

    def open_for_old_ids(self, old_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not old_ids:
            return {}
        marks = ",".join("?" for _ in old_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM pairs WHERE disposition='open' AND old_id IN ({marks})",
                old_ids,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["old_id"], []).append(dict(row))
        return grouped

    def open_pairs(
        self,
        *,
        session_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pairs WHERE disposition='open'"
        params: list[Any] = []
        if session_id:
            query += " AND new_session_id=?"
            params.append(session_id)
        query += " ORDER BY judged_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def open_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM pairs WHERE disposition='open'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def dispose(
        self,
        pair_id: str,
        disposition: str,
        *,
        disposed_by: str | None = None,
    ) -> bool:
        if disposition not in {"confirmed", "dismissed", "obsoleted", "expired"}:
            raise StalenessError(f"invalid disposition: {disposition}")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE pairs SET disposition=?, disposed_by=?, disposed_at=?
                WHERE pair_id=? AND disposition='open'
                """,
                (disposition, disposed_by, _now_iso(), pair_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def close_for_old(
        self,
        old_id: str,
        disposition: str,
        *,
        disposed_by: str | None = None,
    ) -> int:
        """Close all open pairs targeting ``old_id`` (e.g. it was invalidated)."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE pairs SET disposition=?, disposed_by=?, disposed_at=?
                WHERE old_id=? AND disposition='open'
                """,
                (disposition, disposed_by, _now_iso(), old_id),
            )
            self._conn.commit()
        return cursor.rowcount

    def close_for_deleted_memory(self, memory_id: str) -> int:
        """Close open pairs referencing a deleted memory.

        Pairs targeting it are moot (``obsoleted``); pairs whose *new* side
        was deleted lose their evidence source (``expired``).
        """
        now = _now_iso()
        with self._lock:
            a = self._conn.execute(
                "UPDATE pairs SET disposition='obsoleted', disposed_at=? "
                "WHERE old_id=? AND disposition='open'",
                (now, memory_id),
            )
            b = self._conn.execute(
                "UPDATE pairs SET disposition='expired', disposed_at=? "
                "WHERE new_id=? AND disposition='open'",
                (now, memory_id),
            )
            self._conn.commit()
        return a.rowcount + b.rowcount

    def get(self, pair_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pairs WHERE pair_id=?", (pair_id,)
            ).fetchone()
        return dict(row) if row else None
