"""Plan how the memory store is cut up for wiki topic profiling.

A work session is a task unit: the memories written during one session almost
always belong to one or a few tasks, so a session is the natural unit to
profile — and article boundaries follow task boundaries, not the clock.

Three kinds of batch come out of this, and they are NOT interchangeable:

``session``
    One session, small enough to profile in a single call.
``session-part``
    One session too large for a single call, cut into ordered parts. A part is
    never a task on its own: whoever profiles part 2 must be told parts 1 and 3
    exist.
``pack``
    Several small, time-adjacent sessions travelling together to avoid spending
    a call on six memories. They share a call, not a topic: each session in a
    pack still gets its own profile.
``ledger``
    Memories with no real session — the bulk historical import. There is no task
    structure to lean on here, so these chunks need closer reading than a
    session does, and this module only marks them; it does not pretend they are
    sessions.

Ordering is chronological by the batch's first memory, so a reader of the plan
walks the work in the order it happened.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# A batch is sized in memories rather than tokens: memory texts are written to a
# similar length by convention, and a count is inspectable in a way an estimate
# is not. The ceiling exists because the relay drops a request whose first byte
# is slow, and a larger payload is what makes it slow.
DEFAULT_MAX_MEMORIES = 275

# Below this, a session is not worth its own call.
DEFAULT_PACK_THRESHOLD = 60

LEDGER_SOURCE = "agent-memory-ledger"


def _meta(memory: dict[str, Any]) -> dict[str, Any]:
    return memory.get("metadata") or {}


def _created(memory: dict[str, Any]) -> str:
    return (memory.get("created_at") or "")[:19]


def _span(memories: list[dict[str, Any]]) -> list[str]:
    dates = sorted(_created(m)[:10] for m in memories)
    return [dates[0], dates[-1]]


def group_by_session(
    memories: Iterable[dict[str, Any]], ledger_source: str = LEDGER_SOURCE
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split memories into ``{session_id: memories}`` plus the ledger remainder."""
    sessions: dict[str, list[dict[str, Any]]] = {}
    ledger: list[dict[str, Any]] = []
    # Id breaks the tie: memories written in the same second are common, and
    # without it the plan would depend on the order the store happened to
    # return rows in.
    for memory in sorted(memories, key=lambda m: (_created(m), m.get("id") or "")):
        meta = _meta(memory)
        session_id = meta.get("session_id")
        if meta.get("source") == ledger_source or not session_id:
            ledger.append(memory)
        else:
            sessions.setdefault(session_id, []).append(memory)
    return sessions, ledger


def plan_batches(
    memories: Iterable[dict[str, Any]],
    *,
    max_memories: int = DEFAULT_MAX_MEMORIES,
    pack_threshold: int = DEFAULT_PACK_THRESHOLD,
    ledger_source: str = LEDGER_SOURCE,
    key: Callable[[dict[str, Any]], str] = lambda m: m["id"],
) -> list[dict[str, Any]]:
    """Deterministic batch plan. Same input, same plan, every time."""
    sessions, ledger = group_by_session(memories, ledger_source)
    ordered = sorted(sessions.items(), key=lambda item: _created(item[1][0]))

    batches: list[dict[str, Any]] = []
    pending: list[tuple[str, list[dict[str, Any]]]] = []

    def flush_pack() -> None:
        nonlocal pending
        if not pending:
            return
        members = [m for _, group in pending for m in group]
        batches.append({
            "kind": "pack" if len(pending) > 1 else "session",
            "session_ids": [sid for sid, _ in pending],
            "memory_ids": [key(m) for m in members],
            "memory_count": len(members),
            "span": _span(members),
            "sessions": [{"session_id": sid, "memory_ids": [key(m) for m in group],
                          "span": _span(group)} for sid, group in pending],
        })
        pending = []

    for session_id, group in ordered:
        if len(group) > max_memories:
            flush_pack()
            parts = [group[i:i + max_memories] for i in range(0, len(group), max_memories)]
            for number, part in enumerate(parts, start=1):
                batches.append({
                    "kind": "session-part",
                    "session_ids": [session_id],
                    "part": number,
                    "part_count": len(parts),
                    "memory_ids": [key(m) for m in part],
                    "memory_count": len(part),
                    "span": _span(part),
                })
            continue
        if len(group) >= pack_threshold:
            flush_pack()
            pending = [(session_id, group)]
            flush_pack()
            continue
        if sum(len(g) for _, g in pending) + len(group) > max_memories:
            flush_pack()
        pending.append((session_id, group))
    flush_pack()

    for start in range(0, len(ledger), max_memories):
        chunk = ledger[start:start + max_memories]
        batches.append({
            "kind": "ledger",
            "session_ids": [],
            "memory_ids": [key(m) for m in chunk],
            "memory_count": len(chunk),
            "span": _span(chunk),
        })

    for index, batch in enumerate(batches):
        batch["batch_id"] = f"b{index:03d}"
    return batches


def plan_summary(batches: list[dict[str, Any]]) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for batch in batches:
        kinds[batch["kind"]] = kinds.get(batch["kind"], 0) + 1
    profiled = sum(b["memory_count"] for b in batches if b["kind"] != "ledger")
    ledger = sum(b["memory_count"] for b in batches if b["kind"] == "ledger")
    return {
        "batches": len(batches),
        "by_kind": kinds,
        "sessions": len({sid for b in batches for sid in b["session_ids"]}),
        "memories_in_sessions": profiled,
        "memories_in_ledger": ledger,
        "largest_batch": max((b["memory_count"] for b in batches), default=0),
    }
