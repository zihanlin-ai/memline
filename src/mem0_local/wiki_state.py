"""The compile cursor: what a run read, so the next run knows where to start.

Kept as a file rather than in an agent's head because every field here is a
fact about what happened, and a fact an agent has to remember to write down is
a fact that eventually goes unwritten. The first full run of this pipeline
finished without its cursor being updated at all, which would have made the
next incremental compile re-read the entire store.

Three fields, each earning its place:

``last_compile_at``
    The moment the run *started* reading, never the moment it finished.
    Memories written while a run was in flight must belong to the next run,
    and stamping the end time would skip them.
``boundary_memory_ids``
    Everything already read at exactly that timestamp. Second-resolution
    stamps collide often enough that "greater than the cursor" alone either
    re-reads or drops the memories sharing the boundary second.
``source_hashes``
    Content hashes of the designated documents. A document that changed is
    re-profiled; one that did not is skipped, and a document that vanished is
    visible as an id the plan no longer covers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

EMPTY: dict[str, Any] = {"next_run": 1, "last_compile_at": None,
                         "boundary_memory_ids": [], "source_hashes": {}}


def read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return dict(EMPTY)
    try:
        return {**EMPTY, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:  # noqa: BLE001 - an unreadable cursor is a missing cursor
        return dict(EMPTY)


def source_hashes(source_dir: Path) -> dict[str, str]:
    """``{relative path: sha256}`` for every designated Markdown document."""
    if not source_dir.is_dir():
        return {}
    return {str(p.relative_to(source_dir)):
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source_dir.rglob("*.md")) if not p.name.startswith(".")}


def boundary_ids(memories: Iterable[dict[str, Any]], started_at: str) -> list[str]:
    """Ids stamped exactly at the cursor, which the next run must not re-read."""
    from mem0_local.wiki_batch import touched_at

    stamp = started_at[:19]
    return sorted(m["id"] for m in memories if touched_at(m)[:19] == stamp)


def close_run(
    path: Path,
    *,
    started_at: str,
    memories: Iterable[dict[str, Any]],
    source_dir: Path | None = None,
) -> dict[str, Any]:
    """Advance the cursor. Call only when the run actually completed.

    A failed or interrupted run must leave the cursor alone: advancing it
    would silently retire material nothing ever read.
    """
    state = read_state(path)
    new = {
        "next_run": int(state.get("next_run") or 1) + 1,
        "last_compile_at": started_at,
        "boundary_memory_ids": boundary_ids(memories, started_at),
        "source_hashes": source_hashes(source_dir) if source_dir else state.get("source_hashes", {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    return new
