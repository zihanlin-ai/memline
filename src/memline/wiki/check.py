"""Provenance and link checker for the workspace wiki.

Scans formal wiki pages (``content/**/*.md``) for retrieval metadata and provenance and
verifies every reference against current reality:

- ``summary`` and ``topic_key`` must be present on every formal page, so the
  generated index can route a reader without opening every article.

- ``mem:<id>`` refs: the memory must still exist, must not be superseded, and
  its text hash must match the hash recorded at publication time.
- ``sources/<path>[#<heading>]`` refs: the file (and cited section, when
  present) must still exist under the wiki root and its content hash must
  match the recorded hash.
- Body links: relative Markdown links must resolve to existing files.

The checker only reads; it never mutates the wiki or the store. Its report is
compile input: the compile skill turns flags into maintenance suggestions for
the human to review.

Expected page frontmatter (YAML)::

    ---
    title: ...
    summary: ...
    topic_key: stable-page-key
    sources:
      - ref: "mem:0f39f11-..."
        content_hash: "<sha256 of the memory text at publication>"
        historical: true  # optional: explicit retraction/history, not current guidance
      - ref: "sources/plan.md"
        content_hash: "<sha256 of the file at publication>"
    ---

Pages without a ``sources`` block are reported (formal content must carry
provenance) but body-link checking still runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable


from memline.bundle import read_section
from memline.wiki.page import parse_frontmatter, sha256_text

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# [text](target) — capture the target; ignore images ![...](...) too? Images
# are files as well, so check them the same way.
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")

ExecuteFn = Callable[[str, dict[str, Any]], Any]


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
    execute: ExecuteFn,
    memory_id: str,
    recorded_hash: str | None,
    *,
    check_superseded: bool = True,
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
    except Exception as exc:  # noqa: BLE001 - report, don't silently pass
        flags.append({"kind": "memory_status_unverified", "detail": str(exc)})
        return flags
    head_ids = _head_ids(head)
    if head_ids and memory_id not in head_ids and check_superseded:
        flags.append(
            {
                "kind": "memory_superseded",
                "detail": "head: " + ", ".join(sorted(head_ids)),
            }
        )
    return flags


def _head_ids(head: Any) -> set[str]:
    """Collect candidate head memory ids from any reasonable payload shape,
    including ``{"heads": ["<id>", ...]}`` where list items are bare strings."""
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
            if isinstance(item, str):
                ids.add(item)
            else:
                ids |= _head_ids(item)
    return ids


def _check_source_ref(
    wiki_root: Path, ref: str, recorded_hash: str | None
) -> list[dict[str, str]]:
    path_part, _, heading = ref.partition("#")
    path = (wiki_root / path_part).resolve()
    if wiki_root.resolve() not in path.parents:
        return [{"kind": "source_outside_wiki", "detail": ref}]
    if not path.is_file():
        return [{"kind": "source_missing", "detail": ref}]
    text = read_section(path, heading or None)
    if text is None:
        return [{"kind": "source_section_missing", "detail": ref}]
    if recorded_hash:
        actual = sha256_text(text)
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
        # A Blog is a frozen historical record. Its cited memory must still
        # exist and retain its publication-time text, but a later head does not
        # make the historical article stale. Docs represent current guidance,
        # so their provenance must continue to resolve to active heads.
        check_superseded = not rel.startswith("content/blog/")
        markdown = page.read_text(encoding="utf-8")
        front = parse_frontmatter(markdown)
        sources = front.get("sources")
        # A README describes the shelf that holds it rather than asserting
        # anything about the world, so it carries no provenance and is not
        # missing any.
        if page.name != "README.md":
            summary = front.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                flags.append({"page": rel, "kind": "missing_summary", "detail": ""})
            topic_key = front.get("topic_key")
            if not isinstance(topic_key, str) or not topic_key.strip():
                flags.append({"page": rel, "kind": "missing_topic_key", "detail": ""})
            if not (isinstance(sources, list) and sources):
                flags.append({"page": rel, "kind": "missing_provenance", "detail": ""})
                sources = sources if isinstance(sources, list) else []
        for entry in sources or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
                flags.append(
                    {
                        "page": rel,
                        "kind": "malformed_source_entry",
                        "detail": str(entry),
                    }
                )
                continue
            ref = entry["ref"]
            recorded_hash = entry.get("content_hash")
            if not recorded_hash:
                flags.append(
                    {
                        "page": rel,
                        "ref": ref,
                        "kind": "missing_content_hash",
                        "detail": "",
                    }
                )
            if ref.startswith("mem:"):
                historical = entry.get("historical") is True
                found = _check_memory_ref(
                    execute,
                    ref[len("mem:") :],
                    recorded_hash,
                    check_superseded=check_superseded and not historical,
                )
            elif ref.startswith("sources/"):
                found = _check_source_ref(wiki_root, ref, recorded_hash)
            else:
                found = [{"kind": "unknown_ref_scheme", "detail": ref}]
            for flag in found:
                flags.append({"page": rel, "ref": ref, **flag})
        for flag in _check_body_links(page, wiki_root, markdown):
            flags.append({"page": rel, **flag})
    flags += _nav_flags(content_dir / "docs")
    return {
        "wiki_root": str(wiki_root),
        "pages_scanned": len(pages),
        "flag_count": len(flags),
        "flags": flags,
        "clean": not flags,
    }


def _nav_flags(docs_dir: Path) -> list[dict[str, str]]:
    """Routing drift, reported alongside provenance drift.

    A page whose citations all resolve is still lost if the navigation
    skeleton does not reach it, and the two go wrong for the same reason —
    something moved and only half the wiki followed. Keeping them in one
    report is what makes "is this wiki healthy" a single question; the
    standalone ``wiki nav`` remains for iterating on the skeleton itself,
    where checking provenance against the memory store is wasted work.
    """
    from memline.wiki.nav import NAV_FILE, check_nav

    # The skeleton's presence is the request to check it, exactly as a
    # README's markers are the request to maintain its listing. A wiki that
    # routes some other way is not answering this question wrongly; it is not
    # being asked.
    if not (docs_dir / NAV_FILE).is_file():
        return []
    report = check_nav(docs_dir)
    return (
        [{"page": f"content/docs/{p}", "kind": "page_unrouted", "detail": ""}
         for p in report["unreachable"]]
        + [{"page": "content/docs/.nav.yml", "kind": "nav_entry_dangling", "detail": e}
           for e in report["dangling"]]
    )
