"""Maintain the shelf listings the wiki has asked for, and nothing else.

There was once a whole-corpus index here — one file naming every page, on the
theory that at this size reading the list beats searching it. Measured against
real retrieval it did the opposite. A page is found by its path and its
filename; the index only added a large file that matched almost every query
without answering any of them, because a summary tells you a page exists and
not whether it holds your answer. Routing is now the hand-written skeleton at
``content/docs/.nav.yml``, which states entry points and reading order — the
judgement a generated list cannot carry — and filenames do the rest.

What remains is narrow: a shelf whose landing page has asked to have its
children listed gets that listing kept current. Nothing here judges. Titles
were written when the drafts were, summaries are the ``value`` the user
approved when accepting the topic, and status is set by the page check. This
module only reads those and lays them out, so the same content produces
byte-identical output every time.

**It writes into ``content/``, which is otherwise off limits.** That directory
holds approved, published work, and the standing rule is that compile may only
touch a page's ``status`` field. This is the one sanctioned exception and it is
kept narrow in a way a program can enforce: generated text goes between two
markers and nowhere else. No byte outside the markers is ever modified — not
the paragraph someone wrote to explain what a shelf is for, not the
frontmatter, nothing.

A shelf listing is **opt-in**: it is written only into a README that already
exists and already carries the markers. The generator never creates a README
for a directory and never appends markers to one a human wrote without them.
The reason is a rule this wiki takes from vLLM's documentation, whose shape it
is calibrated against: a README exists when the directory has a meaningful
default page — a guide entry, a feature-family overview — and most directories
have none. A generated list of child links in every directory is not
navigation. It is one more file to open before reaching the page, and the
filename it would have pointed at was already visible in the directory. So the
judgement of which directories deserve a landing page stays with the person who
can make it, and the generator maintains the listing only where that person has
asked for one by leaving the markers in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memline.wiki_page import (
    INDEX_BEGIN as BEGIN,
    INDEX_END as END,
    NON_ENTRY,
    parse_frontmatter,
    write_region,
)
from memline.wiki_related import build_related

# States that mean "this page is fine". Anything else is worth a reader's
# attention and is surfaced in the listing.
NORMAL_STATUS = {"published", "current", None, ""}
# Sorts last, so a page that states no order keeps its alphabetical place
# after every page that does.
UNORDERED = 10 ** 9


def _entry(page: Path, root: Path) -> dict[str, Any]:
    front = parse_frontmatter(page.read_text(encoding="utf-8"))
    rel = page.relative_to(root).as_posix()
    order = front.get("order")
    return {
        "path": rel,
        "shelf": rel.rsplit("/", 1)[0] if "/" in rel else "",
        # A page with no title falls back to its filename: a listed shelf must
        # name it either way, because an entry silently dropped for a missing
        # field is worse than an ugly one.
        "title": str(front.get("title") or page.stem),
        "summary": str(front.get("summary") or "").strip(),
        "topic_key": front.get("topic_key"),
        "status": front.get("status"),
        "order": int(order) if isinstance(order, int) else UNORDERED,
    }


def collect(content_dir: Path) -> list[dict[str, Any]]:
    """Every listable page, ordered: explicit ``order`` first, then title."""
    pages = [p for p in sorted(content_dir.rglob("*.md")) if p.name not in NON_ENTRY]
    entries = [_entry(p, content_dir) for p in pages]
    entries.sort(key=lambda e: (e["shelf"], e["order"], e["title"].lower(), e["path"]))
    return entries


def render_entry(entry: dict[str, Any], *, base: str = "") -> str:
    href = entry["path"]
    if base and href.startswith(base + "/"):
        href = href[len(base) + 1:]
    line = f"- [{entry['title']}]({href})"
    if entry["summary"]:
        line += f" — {entry['summary']}"
    # Status rides in the index because that is where a reader looks before
    # opening anything: a page flagged after its evidence moved is worth
    # knowing about *before* it is quoted. Only the abnormal states are shown.
    # Printing `published` on every line was the first thing the generator did
    # against real content, and a marker that appears everywhere is read
    # nowhere.
    if entry["status"] and entry["status"] not in NORMAL_STATUS:
        line += f"  `{entry['status']}`"
    return line


def render_shelf(entries: list[dict[str, Any]], shelf: str) -> str:
    listed = [e for e in entries if e["shelf"] == shelf]
    if not listed:
        return "_No pages published yet._"
    return "\n".join(render_entry(e, base=shelf) for e in listed) + "\n"


def opts_in(readme: Path, *, begin: str = BEGIN) -> bool:
    """Whether this README has asked to have its listing maintained.

    The marker pair is the request. Its absence is an answer too — either the
    directory has no landing page at all, or someone wrote one by hand and did
    not want a generated list bolted onto the end of it.
    """
    return readme.is_file() and begin in readme.read_text(encoding="utf-8")


def refresh(content_dir: Path, **related_kwargs: Any) -> dict[str, Any]:
    """Recompute every generated block in ``content/``: listings and relations.

    They were two commands and that was one too many. Both write between
    markers, both are derived entirely from what the pages already declare,
    and both are only ever correct immediately after a publish — so running
    one without the other leaves the wiki in a state neither command intended.
    A single entry point is what makes "regenerate what is computed" something
    a publisher can do without remembering a list.
    """
    listings = build_index(content_dir)
    relations = build_related(content_dir, **related_kwargs)
    return {
        **listings,
        "written": listings["written"] + relations["written"],
        "relation_pairs": relations["pairs"],
        "pages_with_relations": relations["pages_with_relations"],
    }


def build_index(content_dir: Path) -> dict[str, Any]:
    """Refresh every shelf listing that has opted in. Returns a report."""
    entries = collect(content_dir)
    written: list[str] = []
    shelves = sorted({e["shelf"] for e in entries if e["shelf"]})
    listed = [s for s in shelves if opts_in(content_dir / s / "README.md")]
    for shelf in listed:
        readme = content_dir / shelf / "README.md"
        if write_region(readme, render_shelf(entries, shelf), begin=BEGIN, end=END):
            written.append(f"{shelf}/README.md")
    return {
        "pages": len(entries),
        "shelves": len(shelves),
        # Named so the split is visible: a shelf without a listing is the
        # normal case, not an omission the next run should correct.
        "shelves_listed": listed,
        "written": written,
        # A page with no summary is listed by title alone, which makes the
        # index a directory rather than something you can choose from. Named
        # so it can be fixed, not blocked on.
        "pages_without_summary": [e["path"] for e in entries if not e["summary"]],
        "pages_without_topic_key": [e["path"] for e in entries if not e["topic_key"]],
        "flagged_pages": [{"path": e["path"], "status": e["status"]}
                          for e in entries if e["status"] not in NORMAL_STATUS],
    }
