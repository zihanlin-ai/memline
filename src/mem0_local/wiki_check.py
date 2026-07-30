"""Provenance and link checker for the workspace wiki.

Scans formal wiki pages (``content/**/*.md``) for frontmatter provenance and
verifies every reference against current reality:

- ``mem:<id>`` refs: the memory must still exist, must not be superseded, and
  its text hash must match the hash recorded at publication time.
- ``sources/<path>`` refs: the file must still exist under the wiki root and
  its content hash must match the recorded hash.
- Body links: relative Markdown links must resolve to existing files.

The checker only reads; it never mutates the wiki or the store. Its report is
compile input: the compile skill turns flags into maintenance suggestions for
the human to review.

Expected page frontmatter (YAML)::

    ---
    title: ...
    sources:
      - ref: "mem:0f39f11-..."
        content_hash: "<sha256 of the memory text at publication>"
      - ref: "sources/plan.md"
        content_hash: "<sha256 of the file at publication>"
    ---

Pages without a ``sources`` block are reported (formal content must carry
provenance) but body-link checking still runs.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable

import yaml

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# [text](target) — capture the target; ignore images ![...](...) too? Images
# are files as well, so check them the same way.
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")

ExecuteFn = Callable[[str, dict[str, Any]], Any]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(markdown)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _memory_text(record: Any) -> str | None:
    if isinstance(record, dict):
        for key in ("memory", "text"):
            value = record.get(key)
            if isinstance(value, str):
                return value
        data = record.get("data")
        if isinstance(data, dict):
            return _memory_text(data)
    return None


def _check_memory_ref(
    execute: ExecuteFn, memory_id: str, recorded_hash: str | None
) -> list[dict[str, str]]:
    """Return flag dicts (without page context) for one mem:<id> reference."""
    flags: list[dict[str, str]] = []
    try:
        record = execute("get", {"memory_id": memory_id})
    except Exception as exc:  # noqa: BLE001 - any failure means unresolvable
        return [{"kind": "memory_missing", "detail": str(exc)}]
    text = _memory_text(record)
    if text is None:
        return [{"kind": "memory_missing", "detail": "record has no memory text"}]
    if recorded_hash and sha256_text(text) != recorded_hash:
        flags.append({"kind": "memory_content_changed", "detail": "text hash mismatch"})
    try:
        head = execute("resolve_head", {"memory_id": memory_id})
    except Exception:  # noqa: BLE001 - head resolution is best-effort
        head = None
    head_ids = _head_ids(head)
    if head_ids and memory_id not in head_ids:
        flags.append(
            {"kind": "memory_superseded", "detail": "head: " + ", ".join(sorted(head_ids))}
        )
    return flags


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
            ids |= _head_ids(item)
    return ids


def _check_source_ref(
    wiki_root: Path, ref: str, recorded_hash: str | None
) -> list[dict[str, str]]:
    path = (wiki_root / ref).resolve()
    if wiki_root.resolve() not in path.parents:
        return [{"kind": "source_outside_wiki", "detail": ref}]
    if not path.is_file():
        return [{"kind": "source_missing", "detail": ref}]
    if recorded_hash:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != recorded_hash:
            return [{"kind": "source_content_changed", "detail": ref}]
    return []


def _check_body_links(page: Path, wiki_root: Path, body: str) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    for target in MD_LINK_RE.findall(body):
        if target.startswith(EXTERNAL_PREFIXES) or target.startswith("mem:"):
            continue
        clean = target.split("#", 1)[0]
        if not clean:
            continue
        resolved = (page.parent / clean).resolve()
        try:
            resolved.relative_to(wiki_root.resolve())
        except ValueError:
            flags.append({"kind": "link_outside_wiki", "detail": target})
            continue
        if not resolved.exists():
            flags.append({"kind": "link_broken", "detail": target})
    return flags


def run_check(wiki_root: Path, execute: ExecuteFn) -> dict[str, Any]:
    """Check every formal page under ``<wiki_root>/content``. Read-only."""
    content_dir = wiki_root / "content"
    flags: list[dict[str, str]] = []
    pages = sorted(p for p in content_dir.rglob("*.md")) if content_dir.is_dir() else []
    for page in pages:
        rel = str(page.relative_to(wiki_root))
        markdown = page.read_text(encoding="utf-8")
        front = parse_frontmatter(markdown)
        sources = front.get("sources")
        if page.name != "README.md" and not isinstance(sources, list):
            flags.append({"page": rel, "kind": "missing_provenance", "detail": ""})
            sources = []
        for entry in sources or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
                flags.append({"page": rel, "kind": "malformed_source_entry", "detail": str(entry)})
                continue
            ref = entry["ref"]
            recorded_hash = entry.get("content_hash")
            if ref.startswith("mem:"):
                found = _check_memory_ref(execute, ref[len("mem:"):], recorded_hash)
            elif ref.startswith("sources/"):
                found = _check_source_ref(wiki_root, ref, recorded_hash)
            else:
                found = [{"kind": "unknown_ref_scheme", "detail": ref}]
            for flag in found:
                flags.append({"page": rel, "ref": ref, **flag})
        for flag in _check_body_links(page, wiki_root, markdown):
            flags.append({"page": rel, **flag})
    return {
        "wiki_root": str(wiki_root),
        "pages_scanned": len(pages),
        "flag_count": len(flags),
        "flags": flags,
        "clean": not flags,
    }
