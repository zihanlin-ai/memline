"""Turn an association decision into the suggestion list the user reviews.

The association itself is judgement and belongs to an agent. What happens
afterwards is bookkeeping, and bookkeeping done by hand is where a review list
quietly stops being trustworthy:

* **Evidence is resolved, not copied.** A suggestion cites memories by id and
  records the hash of each one's text as it is now, so a later reader can tell
  whether the material moved under the suggestion.
* **A thread named in an association must exist.** A typo in a thread key would
  otherwise silently shrink a topic's evidence, and the topic would still look
  complete.
* **Verdicts already given are honoured.** A rejected suggestion never returns,
  and one still pending from an earlier run is not proposed a second time.
* **Ids are assigned in one place**, so two agents cannot mint the same one.

The rules about *what* to associate — the Blog session floor, following the
work rather than the calendar, keeping a retraction as one arc — live in the
wiki skill, because they are judgement this module cannot check.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

VERDICT_LINE = re.compile(r"^(\S+)\s*\|\s*(accepted|rejected|deferred|published)\s*\|")


def load_threads(profile_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Every profiled thread, keyed by ``(batch_id, thread_key)``."""
    threads: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(profile_dir.glob("*.json")):
        if path.name.startswith("_") or ".superseded-" in path.name:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("status") != "ok":
            continue
        for session in record.get("profile", {}).get("sessions", []):
            for thread in session.get("threads", []):
                key = (record["batch_id"], thread.get("thread_key") or "")
                thread = {**thread, "session_id": session.get("session_id"),
                          "span": session.get("span"), "batch_id": record["batch_id"],
                          "batch_kind": (record.get("batch") or {}).get("kind")}
                threads[key] = thread
    return threads


def verdicted_ids(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    return {m.group(1) for line in ledger.read_text(encoding="utf-8").splitlines()
            if (m := VERDICT_LINE.match(line.strip()))}


def suppressed_topics(ledger: Path) -> set[str]:
    """Topic keys the user rejected. They do not come back on their own."""
    if not ledger.exists():
        return set()
    out: set[str] = set()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4 and parts[1] == "rejected":
            out.add(parts[0])
    return out


def _evidence(refs: Iterable[str], resolve: Callable[[str], str | None]) -> tuple[list, list]:
    """``(evidence, unresolved)`` — a ref that cannot be resolved is reported."""
    evidence, unresolved = [], []
    for ref in dict.fromkeys(refs):
        if ref.startswith("sources/"):
            evidence.append({"ref": ref, "sha256": None})
            continue
        text = resolve(ref)
        if text is None:
            unresolved.append(ref)
            continue
        evidence.append({"ref": f"mem:{ref}",
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return evidence, unresolved


def build_suggestions(
    associations: list[dict[str, Any]],
    threads: dict[tuple[str, str], dict[str, Any]],
    resolve_memory: Callable[[str], str | None],
    *,
    run: int,
    ledger: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(suggestions, report)``. Never raises on bad input; reports it."""
    already = suppressed_topics(ledger) if ledger else set()
    suggestions: list[dict[str, Any]] = []
    missing_threads: list[dict[str, str]] = []
    unresolved_refs: list[str] = []
    suppressed: list[str] = []
    number = 0

    for topic in associations:
        if topic.get("topic_key") in already:
            suppressed.append(topic["topic_key"])
            continue
        members, refs = [], []
        for member in topic.get("members", []):
            key = (member.get("batch_id"), member.get("thread_key"))
            thread = threads.get(key)
            if thread is None:
                missing_threads.append({"topic_key": topic.get("topic_key"),
                                        "batch_id": key[0], "thread_key": key[1]})
                continue
            members.append({"batch_id": key[0], "session_id": thread.get("session_id"),
                            "thread_key": key[1], "span": thread.get("span")})
            refs += thread.get("evidence_ids") or []
        if not members:
            continue
        evidence, unresolved = _evidence(refs, resolve_memory)
        unresolved_refs += unresolved
        number += 1
        suggestions.append({
            "id": f"run-{run:03d}-S{number:03d}",
            "run": run,
            "type": topic.get("type"),
            "topic_key": topic.get("topic_key"),
            "title": topic.get("title"),
            "value": topic.get("value"),
            "rationale": topic.get("rationale"),
            "member_threads": members,
            "session_count": len({m["session_id"] for m in members}),
            "evidence": evidence,
            "evidence_gaps": topic.get("evidence_gaps") or "none seen",
            "conflicts": topic.get("conflicts") or "none seen",
            "sensitive": topic.get("sensitive") or "none seen",
            "proposed_location": topic.get("proposed_location"),
            "affected_pages": topic.get("affected_pages") or "none",
        })
    report = {
        "suggestions": len(suggestions),
        "by_type": _count(s["type"] for s in suggestions),
        "evidence_refs": sum(len(s["evidence"]) for s in suggestions),
        "single_session_blogs": [s["id"] for s in suggestions
                                 if s["type"] == "blog" and s["session_count"] < 2],
        "suppressed_by_ledger": suppressed,
        "missing_threads": missing_threads,
        "unresolved_evidence": sorted(set(unresolved_refs)),
    }
    return suggestions, report


def _count(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return out
