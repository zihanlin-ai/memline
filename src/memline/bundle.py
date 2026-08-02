"""Package memories for an outbound call to a model outside this machine.

Resolving ids to text is the easy half. The half that needs care is what
leaves: memory text is written for an internal reader and routinely carries
host addresses, account ids and internal URLs that must not reach a third
party. This module replaces those with *stable placeholders* rather than
deleting them — an article about a two-host KV link is unwritable if both
hosts become the same blank, so ``7.150.10.239`` and ``7.150.12.255`` become
``<HOST-1>`` and ``<HOST-2>`` and stay distinguishable throughout the bundle.

The substitution map is returned to the caller, never embedded in the bundle,
so a local reader can restore the real values and a remote model cannot.

Mechanical categories are handled deterministically. Anything that cannot be
recognized by shape — personal names above all — is reported in
``review_flags`` instead of being silently missed: the caller decides whether
to hand-scrub or abandon the bundle. A clean ``review_flags`` is not a proof
of safety, only the absence of the patterns this module knows.

The recorded ``sha256`` is always of the ORIGINAL text, so a bundle's hashes
still match provenance recorded elsewhere against the unsanitized store.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable

ExecuteFn = Callable[[str, dict[str, Any]], Any]

# Ordered: earlier patterns win, so a path's account id is replaced by the
# account rule before the generic path rule can swallow the whole path.
SANITIZE_RULES: tuple[tuple[str, str], ...] = (
    # Internal IPv4. Loopback and unroutable documentation addresses stay:
    # they carry no location and appear in commands a reader must retype.
    ("HOST", r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
    # Account ids of the l00000000 shape, including inside paths and URLs.
    ("USER", r"\b[a-zA-Z]\d{8}\b"),
    # Internal hostnames and the URLs built on them.
    ("INTERNAL_HOST", r"\b[\w.-]+\.(?:huawei\.com|inhuawei\.com|hisilicon\.cn|athuawei\.com)\b"),
    ("INTERNAL_REPO", r"\bhttps?://(?:gitee\.com|gitcode\.com|codehub[\w.-]*)/[\w./-]+"),
    # Container and job identifiers that name a specific run on a specific fleet.
    ("JOB", r"\bmodelarts-job-[\w-]+\b"),
)

# Shapes that are probably sensitive but cannot be replaced safely: a wrong
# guess here corrupts technical meaning, so they are surfaced, not touched.
REVIEW_PATTERNS: tuple[tuple[str, str], ...] = (
    ("cjk_personal_name", r"[一-鿿]{2,4}(?=\s*(?:的|老师|同学)?\b)"),
    ("long_hex_id", r"\b[0-9a-f]{32,}\b"),
    ("email", r"\b[\w.+-]+@[\w.-]+\.\w+\b"),
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Sanitizer:
    """Stable placeholder substitution shared across a whole bundle.

    ``extra`` holds values a human ruled must be replaced but no pattern can
    find — personal names above all. They are applied before the shape rules,
    because a name inside a path has to lose the name before the path rule
    decides what is left.
    """

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self._assigned: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._extra = dict(extra or {})

    def _placeholder(self, category: str, value: str) -> str:
        key = (category, value)
        if key not in self._assigned:
            self._counters[category] = self._counters.get(category, 0) + 1
            self._assigned[key] = f"<{category}-{self._counters[category]}>"
        return self._assigned[key]

    def scrub(self, text: str) -> str:
        for value, category in self._extra.items():
            if value in text:
                text = text.replace(value, self._placeholder(category, value))
        for category, pattern in SANITIZE_RULES:
            text = re.sub(pattern, lambda m: self._placeholder(category, m.group(0)), text)
        return text

    @property
    def mapping(self) -> dict[str, str]:
        """placeholder -> original value. Stays local; never bundled."""
        return {ph: value for (_, value), ph in self._assigned.items()}

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counters)


def review_flags(texts: dict[str, str], reviewed: set[str] | None = None
                 ) -> list[dict[str, str]]:
    """Sensitive-looking shapes this module refuses to guess at.

    ``reviewed`` holds values a human has already ruled on — either cleared as
    a false positive or added to the redaction list. They stop being flagged,
    so the list that remains is only what nobody has looked at yet and a
    caller can meaningfully refuse to send on it.
    """
    reviewed = reviewed or set()
    flags: list[dict[str, str]] = []
    for memory_id, text in texts.items():
        for kind, pattern in REVIEW_PATTERNS:
            for match in dict.fromkeys(re.findall(pattern, text)):
                if match not in reviewed:
                    flags.append({"memory_id": memory_id, "kind": kind, "value": match})
    return flags


def read_section(document: Path, heading: str | None) -> str | None:
    """The text under one Markdown heading, or the whole file when unheaded.

    A source citation points at a section, not a file: an article that cites a
    dependency matrix should carry the matrix, not the whole runbook it lives
    in. The heading is matched on its text so the citation survives a change of
    heading level.
    """
    if not document.is_file():
        return None
    text = document.read_text(encoding="utf-8")
    if not heading:
        return text
    wanted = heading.strip().lower()
    lines = text.splitlines()
    start = None
    depth = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#+)\s+(.*?)\s*$", line)
        if not match:
            continue
        if start is None:
            if match.group(2).strip().lower() == wanted:
                start, depth = index, len(match.group(1))
        elif len(match.group(1)) <= depth:
            return "\n".join(lines[start:index]).strip()
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()


def _resolve_source(root: Path, ref: str) -> dict[str, Any]:
    """One ``sources/<path>#<heading>`` citation as bundle material."""
    path_part, _, heading = ref.partition("#")
    document = (root / path_part).resolve()
    try:
        document.relative_to(root.resolve())
    except ValueError:
        return {"ref": ref, "error": "path escapes the wiki root"}
    section = read_section(document, heading or None)
    if section is None:
        return {"ref": ref, "error": "section not found" if document.is_file() else "file missing"}
    return {"ref": ref, "document": path_part, "heading": heading or None,
            "sha256": hashlib.sha256(section.encode("utf-8")).hexdigest(), "text": section}


def _memory_text(record: Any) -> str | None:
    if isinstance(record, dict):
        for key in ("memory", "text"):
            if isinstance(record.get(key), str):
                return record[key]
        if isinstance(record.get("data"), dict):
            return _memory_text(record["data"])
    return None


def _head_ids(head: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(head, dict):
        for key in ("id", "memory_id"):
            if isinstance(head.get(key), str):
                ids.add(head[key])
        for value in head.values():
            if isinstance(value, (list, dict)):
                ids |= _head_ids(value)
    elif isinstance(head, list):
        for item in head:
            ids |= {item} if isinstance(item, str) else _head_ids(item)
    return ids


def _resolve_one(execute: ExecuteFn, memory_id: str) -> dict[str, Any]:
    """One memory as it should appear in a bundle, or an error entry."""
    try:
        record = execute("get", {"memory_id": memory_id})
    except Exception as exc:  # noqa: BLE001 - an unresolvable id is data, not a crash
        return {"id": memory_id, "error": str(exc)}
    text = _memory_text(record)
    if text is None:
        return {"id": memory_id, "error": "record has no memory text"}
    metadata = record.get("metadata") if isinstance(record, dict) else None
    try:
        superseded = bool(_head_ids(execute("resolve_head", {"memory_id": memory_id})) - {memory_id})
    except Exception:  # noqa: BLE001 - unknown status is reported as unknown
        superseded = None
    return {
        "id": memory_id,
        "created_at": (record.get("created_at") if isinstance(record, dict) else None),
        "writer": (metadata or {}).get("writer_agent_id"),
        "superseded": superseded,
        "sha256": sha256_text(text),
        "text": text,
    }


def build_bundle(
    refs: list[str],
    execute: ExecuteFn,
    *,
    sanitize: bool = True,
    wiki_root: Path | None = None,
    redactions: dict[str, str] | None = None,
    cleared: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(bundle, placeholder_mapping)``. The mapping stays local.

    ``refs`` are citations as an article records them: ``mem:<id>`` or a bare
    memory id for store material, ``sources/<path>#<heading>`` for a designated
    document. Both kinds travel in one bundle because they are equal material —
    a document section is often the settled conclusion a memory only gropes
    towards, and the writer needs both in front of it.
    """
    memory_refs, source_refs = [], []
    for ref in dict.fromkeys(refs):
        (source_refs if ref.startswith("sources/") else memory_refs).append(
            ref[4:] if ref.startswith("mem:") else ref)

    resolved = [_resolve_one(execute, mid) for mid in memory_refs]
    ok = [entry for entry in resolved if "error" not in entry]
    sources = [_resolve_source(wiki_root, ref) for ref in source_refs] if wiki_root else []
    sources_ok = [s for s in sources if "error" not in s]

    reviewed = set(cleared or ()) | set(redactions or {})
    flags = review_flags({entry["id"]: entry["text"] for entry in ok}
                         | {s["ref"]: s["text"] for s in sources_ok}, reviewed)
    sanitizer = Sanitizer(redactions)
    if sanitize:
        for entry in ok:
            entry["text"] = sanitizer.scrub(entry["text"])
        for section in sources_ok:
            section["text"] = sanitizer.scrub(section["text"])
    bundle = {
        "memory_count": len(ok),
        "source_section_count": len(sources_ok),
        "unresolved": [e for e in resolved if "error" in e] + [s for s in sources if "error" in s],
        "sanitized": sanitize,
        "sanitization": {
            "placeholder_counts": sanitizer.counts,
            "review_flags": flags,
        },
        "memories": ok,
        "source_sections": sources_ok,
    }
    return bundle, sanitizer.mapping
