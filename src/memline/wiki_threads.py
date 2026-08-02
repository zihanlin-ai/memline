"""Which sub-topics a draft used, and which it dropped whole.

`wiki_verify` answers whether a citation resolves. It cannot answer the
question a reader actually cares about — *is anything missing* — because it
sees the bundle as a flat set of refs, and a flat set has no notion of a
subject. Dropping forty scattered memories and dropping one investigation are
the same number to it.

Profiling already produced the missing structure. Each thread is a unit of work
with a name, a summary and its own evidence, so a thread whose every member
went uncited is a named thing the article left out. That is the shape of
omission worth surfacing: not "coverage is 0.24" but "the capture shim, the
test plan and the harness audit are absent".

There is no threshold here and there should not be one. Most material does not
deserve to be written down, and a draft that cites a quarter of its evidence
may be exactly right — the ratio says nothing on its own. What this reports is
*which* named threads vanished and how much they carried, so a human can see
the difference between discarding noise and discarding an investigation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from memline.wiki_suggest import load_threads

CITATION = re.compile(r"\^\[(mem:[0-9a-fA-F-]{8,}|sources/[^\]]+)\]")


def cited_refs(draft_markdown: str) -> set[str]:
    return set(CITATION.findall(draft_markdown))


def normalize_evidence(thread: dict[str, Any]) -> set[str]:
    """A thread's members as bundle refs.

    Profiles name memories bare and documents with their ``sources/`` prefix,
    while everything downstream speaks ``mem:<id>``. Normalizing here keeps the
    two vocabularies from silently failing to intersect — which reads as "this
    thread contributed nothing" rather than as the bug it is.
    """
    refs = set()
    for raw in thread.get("evidence_ids") or []:
        if not isinstance(raw, str) or not raw:
            continue
        refs.add(raw if raw.startswith(("mem:", "sources/")) else f"mem:{raw}")
    return refs


def thread_usage(threads: dict[Any, dict[str, Any]], approved: set[str],
                 cited: set[str]) -> dict[str, Any]:
    """Per-thread usage for one draft, and the threads it dropped entirely.

    ``approved`` scopes this to the topic: the store holds hundreds of threads
    that were never this article's to tell, and counting them as dropped would
    bury the ones that were.
    """
    contributing: list[dict[str, Any]] = []
    for thread in threads.values():
        members = normalize_evidence(thread) & approved
        if not members:
            continue
        used = members & cited
        contributing.append({
            "thread_key": thread.get("thread_key"),
            "what": thread.get("what"),
            "outcome": thread.get("outcome"),
            "session_id": thread.get("session_id"),
            "span": thread.get("span"),
            "members": len(members),
            "cited": len(used),
            "refs": sorted(members),
        })
    dropped = [t for t in contributing if t["cited"] == 0]
    dropped.sort(key=lambda t: -t["members"])
    lost = {ref for t in dropped for ref in t["refs"]}
    return {
        "approved_evidence": len(approved),
        "cited_evidence": len(approved & cited),
        "contributing_threads": len(contributing),
        "dropped_threads": len(dropped),
        "evidence_in_dropped_threads": len(lost),
        "share_of_evidence_dropped_whole": round(len(lost) / max(1, len(approved)), 3),
        # Every contributing thread, so a reviewer can also see the ones that
        # survived on a single citation — thinner evidence of the same problem.
        "threads": sorted(contributing, key=lambda t: (t["cited"], -t["members"])),
        "dropped": dropped,
    }


def load_topic(topics_file: Path, topic_key: str) -> dict[str, Any] | None:
    import json

    for line in topics_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if topic_key in (item.get("topic_key"), item.get("id")):
            return item
    return None


def check_draft_threads(draft: Path, topics_file: Path, profile_dirs: list[Path]
                        ) -> dict[str, Any]:
    """``thread_usage`` for one draft on disk, resolved from its accepted topic."""
    topic_key = draft.stem
    topic = load_topic(topics_file, topic_key)
    if topic is None:
        raise ValueError(f"{topic_key}: not in {topics_file}")
    approved = {e["ref"] for e in topic.get("evidence") or []}
    threads = load_threads(*[d for d in profile_dirs if d.is_dir()])
    if not threads:
        raise ValueError(f"no profiled threads under {', '.join(str(d) for d in profile_dirs)}")
    result = thread_usage(threads, approved, cited_refs(draft.read_text(encoding="utf-8")))
    result["draft"] = str(draft)
    result["topic_key"] = topic_key
    return result
