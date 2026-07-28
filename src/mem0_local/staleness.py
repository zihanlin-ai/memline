"""Supersession (staleness) semantics for the local memory store.

Implements the invalidation data model from
``.agents/skills/local-memory/references/staleness-design.md``:

- Memory state lives in payload fields: ``superseded_by`` (list of memory
  ids; absent/empty = active), ``superseded_at``, and ``superseded_reason``.
  Invalidation never touches the memory text or its
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
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from mem0_local.config import STORE_DIR
from mem0_local.llm import active_model
from mem0_local.sqlite_util import SqliteStore

SUPERSEDED_BY = "superseded_by"
SUPERSEDED_AT = "superseded_at"
SUPERSEDED_REASON = "superseded_reason"
TTL_EXPIRES_AT = "ttl_expires_at"
TTL_EXPIRED_AT = "ttl_expired_at"
DISPLACEMENT_PROTECTED_UNTIL = "displacement_protected_until"
DISPLACEMENT_PROTECTION_REASON = "displacement_protection_reason"
DISPLACEMENT_PROTECTED_AT = "displacement_protected_at"
DISPLACEMENT_PROTECTED_BY_AGENT = "displacement_protected_by_agent_id"
DISPLACEMENT_PROTECTED_BY_SESSION = "displacement_protected_by_session_id"

# Suspicion-pair kinds: displacement = new entry displaces an old one;
# necessity = the entry itself may not deserve long-term memory (R1);
# timestamp = the entry's claimed time/actor contradicts CLI metadata (R2);
# ttl_expiry = a TTL deadline fired and awaits review (accept or renew).
KIND_DISPLACEMENT = "displacement"
KIND_NECESSITY = "necessity"
KIND_CORRECTNESS = "correctness"
KIND_TTL_EXPIRY = "ttl_expiry"
KIND_SAFETY = "safety"

# Correctness verdicts that do not apply to imported history: an import's dates
# and actors come from the original ledger, not from the writing session.
# LANGUAGE_SUSPECT is deliberately NOT here — imported Chinese narration embeds
# just as badly as live Chinese narration.
_IMPORT_EXEMPT_VERDICTS = frozenset({"TIMESTAMP_SUSPECT", "ATTRIBUTION_SUSPECT"})

MAX_CHAIN_DEPTH = 100
# Verdicts below the kind's confidence floor are cached (never re-judged)
# but do not open a suspicion for review. Self-checks use a stricter floor:
# eval showed the 0.6-0.8 confidence band is where occasional wrong-direction
# wobble lives, and a borderline self-flag costs reviewer trust.
SUSPICION_CONFIDENCE_FLOOR = 0.6
SELF_CHECK_CONFIDENCE_FLOOR = 0.8
DEFAULT_TTL_DAYS = 7
DEFAULT_DISPLACEMENT_PROTECTION_DAYS = 30
MAX_DISPLACEMENT_PROTECTION_DAYS = 90
MAX_DISPLACEMENT_PROTECTION_REASON_CHARS = 500
REQUIRED_CONSECUTIVE_DISPLACEMENT_FALSE_POSITIVES = 3

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


def is_ttl_expired(payload_or_meta: dict[str, Any] | None, now: str | None = None) -> bool:
    """Lazy TTL check: expired entries leave the pool even before harvest.

    ISO-8601 UTC strings compare lexicographically in chronological order,
    so a plain string comparison is the whole check.
    """
    if not payload_or_meta:
        return False
    expires = payload_or_meta.get(TTL_EXPIRES_AT)
    if not expires:
        return False
    return str(expires) <= (now or _now_iso())


def result_item_expired(item: dict[str, Any], now: str | None = None) -> bool:
    return is_ttl_expired(item.get("metadata") or {}, now) or is_ttl_expired(item, now)


def is_displacement_protected(
    payload_or_meta: dict[str, Any] | None, now: str | None = None
) -> bool:
    """Whether displacement judging is temporarily suppressed for a memory.

    Protection is deliberately narrow: callers use it only when selecting an
    existing memory as a displacement candidate. It never suppresses the
    memory's own necessity, correctness, or safety checks.
    """
    if not payload_or_meta:
        return False
    until = payload_or_meta.get(DISPLACEMENT_PROTECTED_UNTIL)
    return bool(until and str(until) > (now or _now_iso()))


def result_item_displacement_protected(
    item: dict[str, Any], now: str | None = None
) -> bool:
    return is_displacement_protected(
        item.get("metadata") or {}, now
    ) or is_displacement_protected(item, now)


def _point_payload(client: Any, memory_id: str) -> dict[str, Any] | None:
    try:
        point = client.vector_store.get(memory_id)
    except Exception:  # noqa: BLE001 - malformed/unknown ids read as missing.
        return None
    if point is None:
        return None
    payload = getattr(point, "payload", None)
    return dict(payload) if payload else {}


# ---------------------------------------------------------------------------
# Invalidate / revive / resolve-head
# ---------------------------------------------------------------------------


class StalenessError(ValueError):
    """Raised for invalid supersession operations (missing ids, cycles...)."""


def set_displacement_protection(
    client: Any,
    memory_id: str,
    *,
    days: float = DEFAULT_DISPLACEMENT_PROTECTION_DAYS,
    reason: str,
    actor_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Temporarily suppress displacement judging for one existing memory."""
    payload = _point_payload(client, memory_id)
    if payload is None:
        raise StalenessError(f"memory not found: {memory_id}")
    if is_invalidated(payload):
        raise StalenessError("cannot protect an invalidated memory")
    if is_ttl_expired(payload):
        raise StalenessError("cannot protect an expired memory")
    reason = reason.strip()
    if not reason:
        raise StalenessError("protection reason must not be empty")
    if len(reason) > MAX_DISPLACEMENT_PROTECTION_REASON_CHARS:
        raise StalenessError(
            "protection reason must be <= "
            f"{MAX_DISPLACEMENT_PROTECTION_REASON_CHARS} characters"
        )
    if days <= 0 or days > MAX_DISPLACEMENT_PROTECTION_DAYS:
        raise StalenessError(
            f"protection days must be > 0 and <= {MAX_DISPLACEMENT_PROTECTION_DAYS}"
        )
    actor_id = (actor_id or "").strip()
    session_id = (session_id or "").strip()
    if not actor_id or not session_id:
        raise StalenessError(
            "protection requires non-empty actor_id and session_id"
        )

    text = str(payload.get("data") or "")
    evidence = pair_store().consecutive_dismissed_displacement_evidence(
        memory_id,
        text,
        required=REQUIRED_CONSECUTIVE_DISPLACEMENT_FALSE_POSITIVES,
    )
    if not evidence["eligible"]:
        raise StalenessError(
            "protection requires the latest 3 independent displacement "
            "suspicions for this exact text version to all be dismissed "
            f"(consecutive dismissed: {evidence['consecutive_count']})"
        )

    now = _now_iso()
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    patch = {
        DISPLACEMENT_PROTECTED_UNTIL: until,
        DISPLACEMENT_PROTECTION_REASON: reason,
        DISPLACEMENT_PROTECTED_AT: now,
        DISPLACEMENT_PROTECTED_BY_AGENT: actor_id,
        DISPLACEMENT_PROTECTED_BY_SESSION: session_id,
    }
    client.vector_store.update(vector_id=memory_id, vector=None, payload=patch)
    try:
        client.db.add_history(
            memory_id,
            text,
            text,
            "PROTECT_DISPLACEMENT",
            updated_at=now,
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 - audit manifest is authoritative.
        pass
    return {
        "id": memory_id,
        "kind": KIND_DISPLACEMENT,
        "protected_until": until,
        "reason": reason,
        "protected_by_agent_id": actor_id,
        "protected_by_session_id": session_id,
        "dismissed_evidence_count": evidence["consecutive_count"],
        "dismissed_evidence_pair_ids": evidence["pair_ids"],
    }


def clear_displacement_protection(
    client: Any,
    memory_id: str,
    *,
    actor_id: str | None = None,
    cause: str = "manual",
) -> dict[str, Any]:
    """Clear displacement protection; idempotent and payload-only."""
    payload = _point_payload(client, memory_id)
    if payload is None:
        raise StalenessError(f"memory not found: {memory_id}")
    fields = (
        DISPLACEMENT_PROTECTED_UNTIL,
        DISPLACEMENT_PROTECTION_REASON,
        DISPLACEMENT_PROTECTED_AT,
        DISPLACEMENT_PROTECTED_BY_AGENT,
        DISPLACEMENT_PROTECTED_BY_SESSION,
    )
    changed = any(payload.get(field) is not None for field in fields)
    if changed:
        client.vector_store.update(
            vector_id=memory_id,
            vector=None,
            payload={field: None for field in fields},
        )
        now = _now_iso()
        text = str(payload.get("data") or "")
        try:
            client.db.add_history(
                memory_id,
                text,
                text,
                "UNPROTECT_DISPLACEMENT",
                updated_at=now,
                actor_id=actor_id,
            )
        except Exception:  # noqa: BLE001 - audit manifest is authoritative.
            pass
    return {"id": memory_id, "kind": KIND_DISPLACEMENT, "changed": changed, "cause": cause}


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
        # Pool exit ends a narrow review suppression. Otherwise a later
        # revive could unexpectedly resurrect protection granted for an old
        # lifecycle state.
        DISPLACEMENT_PROTECTED_UNTIL: None,
        DISPLACEMENT_PROTECTION_REASON: None,
        DISPLACEMENT_PROTECTED_AT: None,
        DISPLACEMENT_PROTECTED_BY_AGENT: None,
        DISPLACEMENT_PROTECTED_BY_SESSION: None,
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
    """Default search: over-fetch, drop invalidated/expired, flag suspected.

    With ``include_superseded`` the raw result is returned unchanged (full
    history digs). Otherwise invalidated and TTL-expired entries are filtered
    out (over-fetch keeps top_k honest; the TTL check is lazy so correctness
    never depends on the harvest loop) and any hit with open suspicion pairs
    is annotated with ``suspected_stale`` so stale candidates are flagged,
    not silent.
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

    now = _now_iso()
    kept = [
        i for i in items
        if not result_item_superseded(i) and not result_item_expired(i, now)
    ][:top_k]
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
                    "kind": p.get("kind") or KIND_DISPLACEMENT,
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
    self_only: bool = False,
) -> dict[str, Any]:
    """Judge one new entry: necessity, timestamp sanity, then displacement
    against its top-k active neighbors (all advisory only).

    Produces suspicion-pair evidence rows; never changes memory state. Safe
    to re-run: the pair cache skips already-judged (old_id, text-version)
    combinations for every kind.

    ``self_only`` runs just the entry's own necessity/safety/correctness checks
    and skips neighbor displacement. Used by ``update``, where the point is to
    re-judge the rewritten text (three calls) rather than re-scan the store.
    """
    payload = _point_payload(client, new_id)
    if payload is None:
        return {"new_id": new_id, "skipped": "memory no longer exists"}
    if is_invalidated(payload):
        return {"new_id": new_id, "skipped": "memory already invalidated"}
    new_text = str(payload.get("data") or "")
    if not new_text:
        return {"new_id": new_id, "skipped": "empty text"}

    llm = llm or client.llm
    self_report = _run_self_checks(
        client, new_id, new_text, payload,
        llm=llm, judge_model=judge_model, session_id=session_id,
    )
    if self_only:
        return {
            "new_id": new_id, "judged": 0, "opened": 0, "cached": 0,
            "displacement": "skipped (self_only)", **self_report,
        }

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
    now = _now_iso()
    for item in items or []:
        cand_id = str(item.get("id") or "")
        meta = item.get("metadata") or {}
        if (
            not cand_id
            or cand_id == new_id
            or result_item_superseded(item)
            or result_item_displacement_protected(item, now)
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
    already = store.judged_either(
        new_id, new_text, [(c["id"], c["text"]) for c in candidates]
    )
    candidates = [c for c in candidates if c["id"] not in already]
    if not candidates:
        return {
            "new_id": new_id, "judged": 0, "opened": 0, "cached": len(already),
            **self_report,
        }

    from mem0_local.judge import judge as judge_fn

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
            judge_model=active_model(llm, judge_model),
            new_session_id=session_id,
        )
        if row["disposition"] == "open" and row["inserted"]:
            opened += 1
    return {
        "new_id": new_id,
        "judged": len(judgments),
        "opened": opened,
        "cached": len(already),
        **self_report,
    }


def _run_self_checks(
    client: Any,
    new_id: str,
    new_text: str,
    payload: dict[str, Any],
    *,
    llm: Any,
    judge_model: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    """Necessity (R1) and timestamp/attribution (R2) checks on the entry
    itself, recorded as self-pairs (new_id == old_id), version-scoped by the
    entry text so an ``update`` re-arms them. Advisory only; each check is
    skipped when this text version was already judged for that kind."""
    store = pair_store()
    entry = {
        "id": new_id,
        "text": new_text,
        "date": str(payload.get("created_at") or "")[:10],
    }
    report: dict[str, Any] = {}

    if store.has_judgment(new_id, new_id, new_text, kind=KIND_NECESSITY):
        report["necessity"] = "cached"
    else:
        from mem0_local.judge import judge_necessity

        verdict = judge_necessity(llm, entry)
        row = store.record_judgment(
            kind=KIND_NECESSITY,
            new_id=new_id,
            old_id=new_id,
            old_text=new_text,
            verdict=verdict["verdict"],
            confidence=verdict["confidence"],
            reason=verdict["reason"],
            judge_model=active_model(llm, judge_model),
            new_session_id=session_id,
        )
        report["necessity"] = verdict["verdict"]
        report["necessity_open"] = row["disposition"] == "open" and row["inserted"]

    # Safety audit applies to every origin — imported history can leak
    # credentials just as easily as live writes.
    if store.has_judgment(new_id, new_id, new_text, kind=KIND_SAFETY):
        report["safety"] = "cached"
    else:
        from mem0_local.judge import judge_safety

        verdict = judge_safety(llm, entry)
        row = store.record_judgment(
            kind=KIND_SAFETY,
            new_id=new_id,
            old_id=new_id,
            old_text=new_text,
            verdict=verdict["verdict"],
            confidence=verdict["confidence"],
            reason=verdict["reason"],
            judge_model=active_model(llm, judge_model),
            new_session_id=session_id,
        )
        report["safety"] = verdict["verdict"]
        report["safety_open"] = row["disposition"] == "open" and row["inserted"]

    if store.has_judgment(new_id, new_id, new_text, kind=KIND_CORRECTNESS):
        report["correctness"] = "cached"
        return report

    from mem0_local.judge import judge_correctness

    verdict = judge_correctness(
        llm,
        entry,
        ingested_at=str(payload.get("ingested_at") or payload.get("created_at") or ""),
        created_at=str(payload.get("created_at") or "") or None,
        writer=str(payload.get("source") or payload.get("writer_agent_id") or "") or None,
    )
    # Ledger imports carry historical dates and third-party actors by design,
    # so their timestamp/attribution verdicts are noise. That exemption used to
    # skip the whole correctness judge, which silently exempted them from
    # LANGUAGE_SUSPECT too (added later, 2026-07-21) and let imported Chinese
    # narration accumulate unflagged. Now the judge always runs and only the
    # two date/actor verdicts are downgraded for imports.
    if payload.get("origin") == "ledger_import" and verdict["verdict"] in _IMPORT_EXEMPT_VERDICTS:
        verdict = {
            "verdict": "CONSISTENT",
            "confidence": verdict["confidence"],
            "reason": "ledger_import exempt from {}: {}".format(
                verdict["verdict"], verdict["reason"]
            )[:500],
        }
    row = store.record_judgment(
        kind=KIND_CORRECTNESS,
        new_id=new_id,
        old_id=new_id,
        old_text=new_text,
        verdict=verdict["verdict"],
        confidence=verdict["confidence"],
        reason=verdict["reason"],
        judge_model=active_model(llm, judge_model),
        new_session_id=session_id,
    )
    report["correctness"] = verdict["verdict"]
    report["correctness_open"] = row["disposition"] == "open" and row["inserted"]
    return report


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


# Verdicts that open a review suspicion, per pair kind; anything else is a
# cached (never re-judged) verdict for that text version.
_OPENING_VERDICTS: dict[str, Callable[[str], bool]] = {
    KIND_DISPLACEMENT: lambda v: v in {"SUPERSEDED", "DUPLICATE"},
    KIND_NECESSITY: lambda v: v != "DURABLE",
    KIND_CORRECTNESS: lambda v: v != "CONSISTENT",
    KIND_TTL_EXPIRY: lambda v: True,
    KIND_SAFETY: lambda v: v != "CLEAN",
}

# Per-kind floors: a flag below its kind's confidence floor is cached, never
# opened. Necessity/correctness use the strict floor (a borderline self-flag
# costs reviewer trust); safety uses the base floor — missing a leaked
# credential costs more than a spurious review moment.
_KIND_FLOORS: dict[str, float] = {
    KIND_DISPLACEMENT: 0.6,
    KIND_NECESSITY: 0.8,
    KIND_CORRECTNESS: 0.8,
    KIND_SAFETY: 0.6,
    KIND_TTL_EXPIRY: 0.0,
}

_PAIRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    pair_id       TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'displacement',
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
    UNIQUE(kind, new_id, old_id, old_text_hash)
);
CREATE INDEX IF NOT EXISTS idx_pairs_old ON pairs(old_id, disposition);
CREATE INDEX IF NOT EXISTS idx_pairs_new ON pairs(new_id, disposition);
CREATE INDEX IF NOT EXISTS idx_pairs_session ON pairs(new_session_id, disposition);
"""


class PairStore(SqliteStore):
    """Append-only suspicion-pair evidence rows (design §2.2 + lifecycle R1/R2).

    A pair is uniquely identified by ``(kind, new_id, old_id, old_text_hash)``
    — judgments are scoped to a specific version of the old text and expire
    automatically when it changes. Necessity/timestamp checks are self-pairs
    (new_id == old_id). Rows are never deleted; dispositions move ``open``
    pairs to ``confirmed``/``dismissed``/``merged``/``ttl``/``obsoleted``/
    ``expired``.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with self._lock:
            self._migrate_v1_locked()
            self._conn.executescript(_PAIRS_SCHEMA)

    def _migrate_v1_locked(self) -> None:
        """Rebuild a pre-kind table: the old UNIQUE lacked the kind column."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(pairs)")}
        if not cols or "kind" in cols:
            return
        self._conn.executescript(
            "ALTER TABLE pairs RENAME TO pairs_v1;" + _PAIRS_SCHEMA
        )
        self._conn.execute(
            """
            INSERT INTO pairs (
                pair_id, kind, new_id, old_id, old_text_hash, verdict,
                confidence, reason, judged_at, judge_model, new_session_id,
                disposition, disposed_by, disposed_at
            )
            SELECT pair_id, 'displacement', new_id, old_id, old_text_hash,
                   verdict, confidence, reason, judged_at, judge_model,
                   new_session_id, disposition, disposed_by, disposed_at
            FROM pairs_v1
            """
        )
        self._conn.execute("DROP TABLE pairs_v1")
        self._conn.commit()

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
        kind: str = KIND_DISPLACEMENT,
    ) -> dict[str, Any]:
        """Insert one judgment; no-op if this exact pair version was judged."""
        floor = _KIND_FLOORS.get(kind, SELF_CHECK_CONFIDENCE_FLOOR)
        opens = (
            _OPENING_VERDICTS.get(kind, lambda v: False)(verdict)
            and (confidence or 0.0) >= floor
        )
        row = {
            "pair_id": str(uuid.uuid4()),
            "kind": kind,
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
                    pair_id, kind, new_id, old_id, old_text_hash, verdict,
                    confidence, reason, judged_at, judge_model,
                    new_session_id, disposition
                ) VALUES (
                    :pair_id, :kind, :new_id, :old_id, :old_text_hash,
                    :verdict, :confidence, :reason, :judged_at, :judge_model,
                    :new_session_id, :disposition
                )
                """,
                row,
            )
            self._conn.commit()
        row["inserted"] = cursor.rowcount > 0
        return row

    def has_judgment(
        self, new_id: str, old_id: str, old_text: str, *, kind: str
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM pairs WHERE kind=? AND new_id=? AND old_id=? AND old_text_hash=?",
                (kind, new_id, old_id, text_hash(old_text)),
            )
            return cur.fetchone() is not None

    def judged_either(
        self, probe_id: str, probe_text: str, candidates: list[tuple[str, str]]
    ) -> set[str]:
        """Candidate ids already judged against the probe in EITHER direction
        (probe-displaces-candidate or candidate-displaces-probe), version-scoped."""
        probe_hash = text_hash(probe_text)
        judged: set[str] = set()
        with self._lock:
            for cand_id, cand_text in candidates:
                cur = self._conn.execute(
                    "SELECT 1 FROM pairs WHERE kind='displacement' AND ("
                    "(new_id=? AND old_id=? AND old_text_hash=?) OR "
                    "(new_id=? AND old_id=? AND old_text_hash=?))",
                    (probe_id, cand_id, text_hash(cand_text), cand_id, probe_id, probe_hash),
                )
                if cur.fetchone():
                    judged.add(cand_id)
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
        kind: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pairs WHERE disposition='open'"
        params: list[Any] = []
        if session_id:
            query += " AND new_session_id=?"
            params.append(session_id)
        if kind:
            query += " AND kind=?"
            params.append(kind)
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
        if disposition not in {"confirmed", "dismissed", "obsoleted", "expired", "merged", "ttl"}:
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

    def reopen(self, pair_id: str) -> bool:
        """Rollback a disposition whose follow-up store mutation failed.

        Only confirm/merge/ttl have a mutation after disposal. Dismissals and
        lifecycle closures are final for this exact text-versioned pair and
        must never be reopened.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE pairs SET disposition='open', disposed_by=NULL, disposed_at=NULL
                WHERE pair_id=? AND disposition IN ('confirmed', 'merged', 'ttl')
                """,
                (pair_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def close_for_old(
        self,
        old_id: str,
        disposition: str,
        *,
        disposed_by: str | None = None,
        kind: str | None = None,
    ) -> int:
        """Close all open pairs targeting ``old_id`` (e.g. it was invalidated),
        optionally restricted to one suspicion kind."""
        query = "UPDATE pairs SET disposition=?, disposed_by=?, disposed_at=? WHERE old_id=? AND disposition='open'"
        params: list[Any] = [disposition, disposed_by, _now_iso(), old_id]
        if kind:
            query += " AND kind=?"
            params.append(kind)
        with self._lock:
            cursor = self._conn.execute(query, params)
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

    def close_for_updated_text(self, memory_id: str, current_text: str) -> int:
        """Expire open pairs judged against a text version that no longer
        exists (the memory was rewritten via ``update``)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE pairs SET disposition='expired', disposed_at=? "
                "WHERE old_id=? AND disposition='open' AND old_text_hash!=?",
                (_now_iso(), memory_id, text_hash(current_text)),
            )
            self._conn.commit()
        return cursor.rowcount

    def consecutive_dismissed_displacement_evidence(
        self,
        old_id: str,
        old_text: str,
        *,
        required: int = REQUIRED_CONSECUTIVE_DISPLACEMENT_FALSE_POSITIVES,
    ) -> dict[str, Any]:
        """Check the latest opening displacement suspicions for protection.

        Evidence is text-versioned and ordered newest-first. Only a leading
        run of dismissed suspicions counts; any other disposition interrupts
        the run. Independent means each row has a distinct ``new_id``.
        """
        if required <= 0:
            raise StalenessError("required evidence count must be positive")
        with self._lock:
            rows = self._conn.execute(
                "SELECT pair_id, new_id, disposition, judged_at FROM pairs "
                "WHERE kind=? AND old_id=? AND old_text_hash=? "
                "AND verdict IN ('SUPERSEDED', 'DUPLICATE') "
                "AND confidence>=? "
                "ORDER BY judged_at DESC, rowid DESC LIMIT ?",
                (
                    KIND_DISPLACEMENT,
                    old_id,
                    text_hash(old_text),
                    SUSPICION_CONFIDENCE_FLOOR,
                    required,
                ),
            ).fetchall()
        pair_ids: list[str] = []
        new_ids: set[str] = set()
        for row in rows:
            if row["disposition"] != "dismissed" or row["new_id"] in new_ids:
                break
            pair_ids.append(str(row["pair_id"]))
            new_ids.add(str(row["new_id"]))
        count = len(pair_ids)
        return {
            "eligible": count >= required,
            "required": required,
            "consecutive_count": count,
            "pair_ids": pair_ids,
        }

    def get(self, pair_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pairs WHERE pair_id=?", (pair_id,)
            ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# TTL lifecycle (design R3) and delete guard (design §9)
# ---------------------------------------------------------------------------


def set_ttl(
    client: Any,
    memory_id: str,
    *,
    days: float | None = None,
    expires_at: str | None = None,
    clear: bool = False,
    expire_now: bool = False,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Schedule (or clear) reversible pool-exit for one memory.

    Payload-only mutation like invalidate; the search filter honors
    ``ttl_expires_at`` lazily, so the entry leaves the pool at the deadline
    even if no harvest loop ever runs. ``expire_now`` materializes the expiry
    immediately (used by reviewed decisions — necessity confirm, delete
    downgrade) so the harvest loop does not re-open a review flag for a
    call a human already made.
    """
    payload = _point_payload(client, memory_id)
    if payload is None:
        raise StalenessError(f"memory not found: {memory_id}")
    now = _now_iso()
    if clear:
        patch: dict[str, Any] = {TTL_EXPIRES_AT: None, TTL_EXPIRED_AT: None}
        result = {"id": memory_id, "ttl_cleared": True}
    elif expire_now:
        patch = {
            TTL_EXPIRES_AT: now,
            TTL_EXPIRED_AT: now,
            "ttl_set_at": now,
            "ttl_set_by": actor_id,
        }
        result = {"id": memory_id, TTL_EXPIRES_AT: now, TTL_EXPIRED_AT: now}
    else:
        if expires_at is None:
            delta = timedelta(days=days if days is not None else DEFAULT_TTL_DAYS)
            expires_at = (datetime.now(timezone.utc) + delta).isoformat()
        # Setting a new deadline is also the renewal path: clear any
        # materialized expiry so the entry re-enters the pool.
        patch = {
            TTL_EXPIRES_AT: expires_at,
            TTL_EXPIRED_AT: None,
            "ttl_set_at": now,
            "ttl_set_by": actor_id,
        }
        result = {"id": memory_id, TTL_EXPIRES_AT: expires_at}
    client.vector_store.update(vector_id=memory_id, vector=None, payload=patch)
    # Any open expiry flag is moot once the deadline is cleared or renewed —
    # every renewal path must close it, not just `stale ttl`, or review keeps
    # advising about an entry that is already back in the pool.
    try:
        pair_store().close_for_old(
            memory_id, "ttl", disposed_by=actor_id, kind=KIND_TTL_EXPIRY
        )
    except Exception:  # noqa: BLE001 - flag hygiene must never break set_ttl.
        pass
    text = str(payload.get("data") or "")
    event = "TTL_CLEAR" if clear else ("TTL_EXPIRE" if expire_now else "TTL_SET")
    try:
        client.db.add_history(
            memory_id, text, text, event, updated_at=now, actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 - history is best-effort.
        pass
    return result


def harvest_expired(client: Any, *, now: str | None = None, limit: int = 1000) -> dict[str, Any]:
    """Materialize lazy TTL expiries: stamp ``ttl_expired_at``, close open
    suspicions on the entry, write a history event, and open a ``ttl_expiry``
    review flag so a later session decides — accept the expiry or renew the
    TTL. Correctness never depends on this running — the search filter is
    already lazy."""
    now = now or _now_iso()
    try:
        from mem0_local.config import DEFAULT_USER_ID

        # mem0's get_all requires a scope key alongside custom filters.
        raw = client.get_all(
            filters={"user_id": DEFAULT_USER_ID, TTL_EXPIRES_AT: {"lte": now}},
            top_k=limit,
        )
    except Exception as exc:  # noqa: BLE001 - range filter support may vary.
        return {"harvested": 0, "error": f"ttl scan failed: {exc}"}
    items = raw.get("results") if isinstance(raw, dict) else raw
    harvested = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("id") or "")
        meta = item.get("metadata") or {}
        if not memory_id or meta.get(TTL_EXPIRED_AT):
            continue
        if not is_ttl_expired(meta, now) and not is_ttl_expired(item, now):
            continue
        client.vector_store.update(
            vector_id=memory_id, vector=None, payload={TTL_EXPIRED_AT: now}
        )
        try:
            client.db.add_history(
                memory_id, str(item.get("memory") or ""), str(item.get("memory") or ""),
                "TTL_EXPIRE", updated_at=now,
            )
        except Exception:  # noqa: BLE001
            pass
        expires = str(meta.get(TTL_EXPIRES_AT) or item.get(TTL_EXPIRES_AT) or "")
        try:
            store = pair_store()
            # Close other open suspicions first so they cannot swallow the
            # fresh expiry flag; the flag's text is salted with the deadline
            # so each renewal cycle re-arms exactly one new flag.
            store.close_for_old(memory_id, "expired")
            store.record_judgment(
                kind=KIND_TTL_EXPIRY,
                new_id=memory_id,
                old_id=memory_id,
                old_text=f"{item.get('memory') or ''}@@{expires}",
                verdict="TTL_EXPIRED",
                confidence=1.0,
                reason=(
                    f"ttl expired at {expires or now}; the entry left the search "
                    "pool. `stale confirm` accepts the expiry, `stale ttl` renews."
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        harvested += 1
    return {"harvested": harvested}


def delete_guard(client: Any, memory_id: str) -> dict[str, Any]:
    """Check whether a memory participates in any supersession edge.

    Chain participants must never be hard-deleted (dangling ``superseded_by``
    pointers break ``resolve_head``); callers downgrade to reversible expiry
    instead. Fails open on scan errors: the guard is an extra safety, not a
    gate the store's health depends on.
    """
    payload = _point_payload(client, memory_id)
    if payload is None:
        return {"participates": False, "reason": "memory not found"}
    if superseded_ids(payload):
        return {"participates": True, "reason": "memory is superseded (chain node)"}
    try:
        raw = client.get_all(filters={SUPERSEDED_BY: memory_id}, top_k=1)
        items = raw.get("results") if isinstance(raw, dict) else raw
        if items:
            return {"participates": True, "reason": "memory supersedes other entries"}
    except Exception:  # noqa: BLE001 - fail open.
        pass
    return {"participates": False, "reason": None}
