"""The handoff verdict: is this session allowed to say it is done?

The rule this module owns is disposition authority, and it is worth a module
because it is easy to get wrong in both directions. A session disposes only
its own writes — pairs raised by other sessions never block its pass, or a
shared store would make the verdict unreachable. TTL expiries are the one
exception: authority over them is granted to every session, so they are
nobody's property and would otherwise be nobody's obligation; they block
everyone precisely so that they drain.

The verdict is computed, never inferred: "pass" is the absence of every named
blocker, so a session never has to reconstruct "am I done?" from four lists.
And each finding travels with its playbook entry — what the verdict means,
the fix that resolves it, the one condition under which dismissing is right —
because a reviewer handed a bare verdict reconstructs the handling rule from
memory, and the reconstruction is where wrong dispositions come from.

Everything here takes its collaborators as arguments. That is what let this
logic leave the CLI: given a fake store and a fake queue, the authority rule
is a unit test, not a shell session.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from memline.runtime import normalize_items

# Reviewer guidance keyed by verdict: what the finding means, the disposition
# that resolves it, and the one condition under which dismissing is right.
VERDICT_PLAYBOOK: dict[str, dict[str, str]] = {
    "LANGUAGE_SUSPECT": {
        "means": "the entry's own narration is Chinese; the store embeds with an "
                 "English-only model, so this text retrieves poorly",
        "fix": "update <memory_id> '<same facts, narration rewritten in English>'",
        "keep": "preserve verbatim: paths, commands, flags, hosts, model/artifact "
                "names, numbers, memory-id references, Chinese proper nouns "
                "(people, products, filenames), and quoted Chinese source material",
        "dismiss_only_if": "the Chinese is CONFINED to quotes, identifiers, proper "
                           "nouns or a one-word gloss and the author's own sentences "
                           "are already English — not merely because the facts are correct",
    },
    "TIMESTAMP_SUSPECT": {
        "means": "the narrated date contradicts the CLI's authoritative ingestion time",
        "fix": "update <memory_id> '<same facts, date corrected>'",
        "dismiss_only_if": "the entry narrates a historical event under its own past "
                           "date, or carries an 'As of/REVISED <date>' status marker",
    },
    "ATTRIBUTION_SUSPECT": {
        "means": "the entry credits an actor that contradicts the recorded writer identity",
        "fix": "update <memory_id> '<same facts, actor corrected>'",
        "dismiss_only_if": "the entry legitimately records the user's decision or "
                           "another agent's action",
    },
    "SECRET_SUSPECT": {
        "means": "PRIORITY — the entry looks like it embeds a live credential VALUE",
        "fix": "update <memory_id> '<same text, secret replaced by a pointer to its "
               "file or secret location>' (keeps id, links and history), then verify "
               "`search '<secret literal>' --include-superseded` returns nothing",
        "dismiss_only_if": "the string is not a credential value (e.g. a public "
                           "identifier, a placeholder, or a path to where a secret lives)",
    },
    "BORN_UNNECESSARY": {
        "means": "the entry may never have deserved long-term memory — activity "
                 "narration, a commit restatement, or a repo-readable fact",
        "fix": "stale confirm <pair_id> (expires it now, reversible via "
               "`ttl <memory_id> --clear`), or `delete <memory_id>` for your own fresh entry",
        "partial": "keep only the non-derivable part -> update <memory_id> '<rewritten>'",
        "dismiss_only_if": "the entry records a durable decision, constraint or "
                           "hard-won conclusion that is not recoverable from the repo",
    },
    "EXPIRING": {
        "means": "the entry is time-scoped — a progress tick or event-scoped coordination",
        "fix": "settled/closed -> stale confirm <pair_id> (expires now); still alive -> "
               "stale ttl <pair_id> [--days 7]",
        "dismiss_only_if": "the fact turned out to be durable rather than event-scoped",
    },
    "SUPERSEDED": {
        "means": "a newer entry appears to replace this one's answer",
        "fix": "stale confirm <pair_id> (retires the old entry, pointing at the new one)",
        "partial": "the new entry ADDS detail rather than replacing -> "
                   "stale merge <pair_id> '<consolidated text>'",
        "dismiss_only_if": "both entries remain independently true (different scope, "
                           "config or time window)",
    },
    "DUPLICATE": {
        "means": "a newer entry states the same fact as this one",
        "fix": "stale confirm <pair_id> (retires the redundant older entry)",
        "dismiss_only_if": "the entries differ in a way that matters for retrieval",
    },
    "TTL_EXPIRED": {
        "means": "this entry's TTL deadline fired and it has already left the search pool",
        "fix": "accept the expiry -> stale confirm <pair_id>",
        "partial": "still needed -> stale ttl <pair_id> [--days 7] (renews, re-enters the pool)",
        "dismiss_only_if": "never — a fired TTL is a fact, not a judgment; confirm or renew it",
    },
}

# Disposition order (design 2026-07-21): safety first (redact before anything
# else can invalidate and strand the plaintext), then correctness (fix the
# fact), then necessity (may then invalidate). Displacement is handled last.
_SELF_FLAG_ORDER = {"safety": 0, "correctness": 1, "necessity": 2}

HOW_TO_DISPOSE = (
    "every listed pair carries a `suggested` block: what the verdict "
    "means, the `fix` that resolves it, and `dismiss_only_if` — the "
    "one condition under which dismissing is right. Read it per pair; "
    "the summary below is only the ordering. "
    "self_flags are ordered by disposition priority: safety -> "
    "correctness -> necessity. "
    "safety: update <memory_id> '<secret replaced by its location pointer>' "
    "(redact in place, then verify search '<secret>' --include-superseded is empty) | stale dismiss <pair_id>. "
    "correctness: update <memory_id> '<corrected>' | stale dismiss <pair_id>. "
    "necessity: stale confirm (expire now) | stale ttl <pair_id> [--days 7] | "
    "delete <memory_id> (own fresh born-unnecessary entry) | stale dismiss. "
    "displacement (handle last): stale confirm <pair_id> | stale dismiss <pair_id> | "
    "stale merge <pair_id> '<consolidated text>' | update <old_id> '<corrected text>'. "
    "ttl_expiry: stale confirm (accept) | stale ttl <pair_id> (renew, re-enters pool)."
)

ExecuteFn = Callable[[str, dict[str, Any]], Any]


def flag_suggestion(pair: dict[str, Any]) -> dict[str, Any]:
    """Reviewer guidance for one suspicion, keyed by its verdict.

    Returns the judged finding plus the disposition that resolves it, so a
    reviewer never has to reconstruct the handling rule from the kind alone.
    """
    kind = pair.get("kind") or "displacement"
    verdict = str(pair.get("verdict") or "")
    play = VERDICT_PLAYBOOK.get(verdict)
    if play is None:
        play = {
            "means": f"{kind} suspicion ({verdict or 'unknown verdict'})",
            "fix": "update <memory_id> '<corrected>' | stale confirm <pair_id>",
            "dismiss_only_if": "the finding is a false positive",
        }
    out: dict[str, Any] = {"verdict": verdict, **play}
    out["dismiss"] = "stale dismiss <pair_id>"
    out["warning"] = (
        "dismissal is PERMANENT for this exact text version — a dismissed flag "
        "cannot be reopened unless the memory's text changes"
    )
    return out


def fetch_memory(execute: ExecuteFn, memory_id: str) -> dict[str, Any] | None:
    try:
        row = execute("get", {"memory_id": memory_id})
        return row if isinstance(row, dict) else None
    except Exception:  # noqa: BLE001 - preview only.
        return None


def enrich_pairs(execute: ExecuteFn, pairs: list[dict[str, Any]], *,
                 preview_chars: int = 160) -> list[dict[str, Any]]:
    enriched = []
    for pair in pairs:
        item = dict(pair)
        for side in ("old", "new"):
            row = fetch_memory(execute, pair[f"{side}_id"])
            item[f"{side}_memory"] = (
                str(row.get("memory") or "")[:preview_chars] if row else "<deleted>"
            )
        item["suggested"] = flag_suggestion(pair)
        enriched.append(item)
    return enriched


def _flag_view(execute: ExecuteFn, pair: dict[str, Any]) -> dict[str, Any]:
    row = fetch_memory(execute, pair["old_id"])
    return {
        "pair_id": pair["pair_id"],
        "kind": pair["kind"],
        "memory_id": pair["old_id"],
        "verdict": pair["verdict"],
        "confidence": pair["confidence"],
        "reason": pair["reason"],
        "memory": str(row.get("memory") or "")[:160] if row else "<deleted>",
        "suggested": flag_suggestion(pair),
    }


def session_review(
    session: str,
    *,
    execute: ExecuteFn,
    queue_factory: Callable[[], Any],
    pairs: Any,
    user_id: str,
    wait: bool = False,
    wait_seconds: float = 120.0,
    poll_seconds: float = 3.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """This session's writes, the suspicions they raised, and the verdict.

    "pass" only when everything THIS session wrote has been handled — its
    writes' own safety/correctness/necessity flags, the retirements its
    writes raised, and any queued write of its own that failed to land — and
    no TTL expiry is left unreviewed. Hard-coded blockers, so a session never
    has to infer "am I done?" from four lists.
    """

    def pending_stale_checks() -> int:
        queue = queue_factory()
        count = 0
        for row in queue.list(limit=500):
            if row["op"] == "stale_check" and row["status"] in {"queued", "processing"}:
                args = queue.get_args(row["event_id"]) or {}
                if args.get("session_id") == session:
                    count += 1
        return count

    def failed_own_adds() -> list[str]:
        queue = queue_factory()
        out: list[str] = []
        for row in queue.list(status="failed", limit=500):
            if row["op"] != "add" or row.get("acked"):
                continue
            args = queue.get_args(row["event_id"]) or {}
            if args.get("session_id") == session:
                out.append(row["event_id"])
        return out

    pending = pending_stale_checks()
    if wait:
        deadline = clock() + wait_seconds
        while pending and clock() < deadline:
            sleep(poll_seconds)
            pending = pending_stale_checks()

    writes = execute("list", {"filters": {"user_id": user_id, "run_id": session},
                              "top_k": 500, "start": 0, "end": 500})
    write_items = normalize_items(writes) or (writes if isinstance(writes, list) else [])

    all_pairs = pairs.open_pairs(session_id=session)
    displacement = [p for p in all_pairs
                    if (p.get("kind") or "displacement") == "displacement"]
    self_flags = sorted(
        (_flag_view(execute, p) for p in all_pairs
         if (p.get("kind") or "displacement") not in {"displacement", "ttl_expiry"}),
        key=lambda f: _SELF_FLAG_ORDER.get(f["kind"], 9),
    )
    # TTL expiries are lifecycle events, not this session's writes: any
    # session running review may dispose them (accept or renew).
    ttl_expired = [_flag_view(execute, p) for p in pairs.open_pairs(kind="ttl_expiry")]

    blocking: list[dict[str, Any]] = []
    if pending:
        blocking.append({
            "kind": "pending_stale_checks",
            "count": pending,
            "why": "this session's writes are still being judged; a verdict now would be premature",
            "how": "memline review --wait",
        })
    for flag_kind in ("safety", "correctness", "necessity"):
        flagged = [f["memory_id"] for f in self_flags if f["kind"] == flag_kind]
        if flagged:
            blocking.append({
                "kind": f"self_flag:{flag_kind}",
                "count": len(flagged),
                "memory_ids": flagged,
                "why": f"this session's own writes carry unresolved {flag_kind} flags",
                "how": "see how_to_dispose",
            })
    if displacement:
        blocking.append({
            "kind": "displacement_raised_by_me",
            "count": len(displacement),
            "pair_ids": [p["pair_id"] for p in displacement],
            "why": "retirements that this session's writes raised are still undisposed",
            "how": "stale confirm|dismiss|merge <pair_id>, or update <old_id>",
        })
    if ttl_expired:
        blocking.append({
            "kind": "ttl_expired",
            "count": len(ttl_expired),
            "pair_ids": [p["pair_id"] for p in ttl_expired],
            "why": "entries whose TTL fired are unreviewed; any session may dispose them, "
                   "so this one is obliged to",
            "how": "stale confirm <pair_id> (accept the expiry) | stale ttl <pair_id> --days N (renew)",
        })
    failed_adds = failed_own_adds()
    if failed_adds:
        blocking.append({
            "kind": "failed_adds",
            "count": len(failed_adds),
            "event_ids": failed_adds,
            "why": "this session queued writes that never landed in the store",
            "how": "event retry <event_id> | event ack <event_id>",
        })

    return {
        "session": session,
        "verdict": "pass" if not blocking else "blocked",
        "blocking": blocking,
        "writes_count": len(write_items),
        "writes": [{"id": w.get("id"), "memory": str(w.get("memory") or "")[:160]}
                   for w in write_items],
        "pending_stale_checks": pending,
        "open_pairs": enrich_pairs(execute, displacement),
        "self_flags": self_flags,
        "ttl_expired": ttl_expired,
        "how_to_dispose": HOW_TO_DISPOSE,
    }
