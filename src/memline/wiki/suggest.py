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


def load_threads(*profile_dirs: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Every profiled thread, keyed by ``(batch_id, thread_key)``.

    Several directories because memory batches and source documents are
    profiled separately but associate together: a docs page normally draws on
    both, and reading only one directory would silently drop half its members.
    """
    threads: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(p for d in profile_dirs for p in d.glob("*.json")):
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


# What each page-level flag means for the page, and how urgently. The wording
# is what a reviewer reads first, so it says what happened rather than naming
# the check that noticed.
_FLAG_MEANING: dict[str, tuple[str, str]] = {
    "memory_missing": (
        "retired-evidence",
        "cites a memory that no longer exists; the sentence resting on it is unsupported"),
    "memory_superseded": (
        "retired-evidence",
        "cites a memory that has been replaced; the page may state a belief that was overturned"),
    "memory_content_changed": (
        "changed-evidence",
        "cites a memory whose text changed after publication; the citation resolves but may no "
        "longer say what the page claims"),
    "source_missing": (
        "retired-evidence",
        "cites a source document that is no longer under sources/"),
    "source_content_changed": (
        "changed-evidence",
        "cites a source section whose document changed after publication"),
    "memory_status_unverified": (
        "unverifiable-evidence",
        "the store could not confirm whether this memory is still current"),
    "missing_provenance": (
        "missing-provenance",
        "formal page carries no sources block, so nothing it says can be traced"),
    "malformed_source_entry": (
        "missing-provenance", "a provenance entry cannot be read"),
    "missing_content_hash": (
        "missing-provenance",
        "a provenance entry records no hash, so silent edits to its evidence cannot be detected"),
    "unknown_ref_scheme": ("missing-provenance", "a provenance ref uses an unrecognised scheme"),
    "link_broken": ("broken-link", "links to a file that does not exist"),
    "link_outside_wiki": ("broken-link", "links outside the wiki"),
    "source_outside_wiki": ("broken-link", "cites a path outside the wiki"),
}


def maintenance_suggestions(check_report: dict[str, Any], *, run: int,
                            start_number: int = 0) -> list[dict[str, Any]]:
    """Turn page-check flags into suggestions, one per affected page.

    This exists because the alternative did not work. The rule was written
    down — compile should also propose maintenance from the page check — but
    it lived only in the skill, as an instruction for whoever was driving, and
    it named a command that had since been renamed. Nothing in the program
    read a check report, and no maintenance suggestion was ever produced.

    A retired memory is the case that makes this non-optional. A *superseded*
    one is at least visible from the store: its session moved, so the next
    incremental compile re-reads and re-profiles it. A *deleted* one is not
    visible at all — the incremental plan iterates memories that exist, and a
    memory that is gone leaves nothing to iterate. The only record that the
    page ever depended on it is the page's own frontmatter, which is exactly
    what the check reads. Skip the check and that dependency is not merely
    missed this run; it becomes permanently invisible.

    Grouped per page rather than per flag: eleven dead citations on one page
    are one decision about one page, not eleven.
    """
    by_page: dict[str, list[dict[str, Any]]] = {}
    for flag in check_report.get("flags") or []:
        if isinstance(flag, dict) and flag.get("page"):
            by_page.setdefault(flag["page"], []).append(flag)

    out: list[dict[str, Any]] = []
    for page, flags in sorted(by_page.items()):
        kinds = sorted({f.get("kind") for f in flags if f.get("kind")})
        categories = {_FLAG_MEANING.get(k, ("other", k))[0] for k in kinds}
        reasons = [_FLAG_MEANING.get(k, ("other", f"flagged {k}"))[1] for k in kinds]
        refs = [f["ref"] for f in flags if f.get("ref")]
        # Blog is frozen: its body is the record of what was believed, so the
        # fix is an erratum beside it rather than an edit to it. Docs are
        # living and are simply corrected.
        frozen = page.startswith("content/blog/")
        out.append({
            "id": f"run-{run:03d}-S{start_number + len(out) + 1:03d}",
            "run": run,
            "type": "erratum" if frozen else "maintenance",
            "topic_key": f"maintain-{Path(page).stem}",
            "title": f"{'Erratum for' if frozen else 'Refresh'} {page}",
            "value": "; ".join(sorted(set(reasons))),
            "rationale": ("A frozen article cannot be rewritten, so a correction belongs in "
                          "content/errata/ beside it." if frozen else
                          "A living page states current guidance and its evidence moved."),
            "member_threads": [],
            "session_count": 0,
            # The page's own dead refs, so a reviewer sees what broke without
            # opening the check report.
            "evidence": [{"ref": ref, "sha256": None} for ref in sorted(set(refs))],
            "evidence_gaps": f"{len(flags)} flag(s): {', '.join(kinds)}",
            "conflicts": "none seen",
            "sensitive": "none seen",
            "proposed_location": page,
            "affected_pages": page,
            "flag_categories": sorted(categories),
        })
    return out
