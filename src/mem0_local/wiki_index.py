"""Generate the wiki's index from what the pages already declare.

An eighty-page wiki does not need search. It needs one file an agent can read
whole — every page's title, what it is for, and whether it is still trusted —
because at that size reading the list is cheaper and more accurate than
matching against it. The comparison that settled this is vLLM's: 243 pages
navigated by a sixty-line table of contents, no embeddings anywhere.

Nothing here judges. Titles were written when the drafts were, summaries are
the ``value`` the user approved when accepting the topic, and status is set by
the page check. This module only reads those and lays them out, so the same
content produces byte-identical output every time.

**It writes into ``content/``, which is otherwise off limits.** That directory
holds approved, published work, and the standing rule is that compile may only
touch a page's ``status`` field. This is the one sanctioned exception and it is
kept narrow in a way a program can enforce: generated text goes between two
markers and nowhere else, and on a page that has no markers yet the block is
appended. No byte outside the markers is ever modified — not the paragraph
someone wrote to explain what a shelf is for, not the frontmatter, nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mem0_local.wiki_check import parse_frontmatter

BEGIN = "<!-- index:begin -->"
END = "<!-- index:end -->"
GENERATED_REGION = re.compile(
    re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
# Pages that describe the wiki rather than belonging to it.
NON_ENTRY = {"README.md", "INDEX.md"}
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
        # A page with no title falls back to its filename: the index must list
        # it either way, because a page missing from the index is a page that
        # does not exist as far as retrieval is concerned.
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


def shelf_title(content_dir: Path, shelf: str) -> str:
    """The shelf's own H1, so the index uses the name a human chose for it."""
    readme = content_dir / shelf / "README.md" if shelf else content_dir / "README.md"
    if readme.is_file():
        for line in readme.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return shelf or "Wiki"


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


def render_index(entries: list[dict[str, Any]], content_dir: Path) -> str:
    """The root listing: every page in the wiki, grouped by shelf."""
    if not entries:
        return "_No pages published yet._"
    lines: list[str] = []
    for shelf in sorted({e["shelf"] for e in entries}):
        lines.append(f"### {shelf_title(content_dir, shelf)}")
        lines.append("")
        lines += [render_entry(e) for e in entries if e["shelf"] == shelf]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_shelf(entries: list[dict[str, Any]], shelf: str) -> str:
    listed = [e for e in entries if e["shelf"] == shelf]
    if not listed:
        return "_No pages published yet._"
    return "\n".join(render_entry(e, base=shelf) for e in listed) + "\n"


def write_region(path: Path, body: str, *, preamble: str = "") -> bool:
    """Replace the marked region in ``path``. Returns whether anything changed.

    Everything outside the markers is preserved exactly. A file without markers
    gains the block at the end — appended, never merged into what is there —
    and a file that does not exist is created from ``preamble``.
    """
    block = f"{BEGIN}\n{body.rstrip()}\n{END}"
    if path.is_file():
        current = path.read_text(encoding="utf-8")
        if GENERATED_REGION.search(current):
            updated = GENERATED_REGION.sub(lambda _: block, current, count=1)
        else:
            updated = current.rstrip("\n") + "\n\n" + block + "\n"
    else:
        updated = (preamble.rstrip("\n") + "\n\n" if preamble else "") + block + "\n"
    if path.is_file() and updated == path.read_text(encoding="utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return True


ROOT_PREAMBLE = """# Wiki Index

Every published page, with what it is for. Generated from page frontmatter —
edit the pages, not this list. A page marked `stale-pending-review` cites
evidence that has moved since publication and is not current fact until
someone rules on it."""


def build_index(content_dir: Path) -> dict[str, Any]:
    """Write the root index and each shelf's listing. Returns a report."""
    entries = collect(content_dir)
    written: list[str] = []
    if write_region(content_dir / "INDEX.md", render_index(entries, content_dir),
                    preamble=ROOT_PREAMBLE):
        written.append("INDEX.md")
    for shelf in sorted({e["shelf"] for e in entries if e["shelf"]}):
        readme = content_dir / shelf / "README.md"
        if write_region(readme, render_shelf(entries, shelf)):
            written.append(f"{shelf}/README.md")
    return {
        "pages": len(entries),
        "shelves": len({e["shelf"] for e in entries if e["shelf"]}),
        "written": written,
        # A page with no summary is listed by title alone, which makes the
        # index a directory rather than something you can choose from. Named
        # so it can be fixed, not blocked on.
        "pages_without_summary": [e["path"] for e in entries if not e["summary"]],
        "pages_without_topic_key": [e["path"] for e in entries if not e["topic_key"]],
        "flagged_pages": [{"path": e["path"], "status": e["status"]}
                          for e in entries if e["status"] not in NORMAL_STATUS],
    }
