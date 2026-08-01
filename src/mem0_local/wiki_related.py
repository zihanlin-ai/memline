"""Relate pages by the evidence they share, without asking a model.

Two pages built from overlapping evidence are related by construction, and the
overlap is already recorded: every published page lists the refs it was written
from. So this relation costs nothing to compute, cannot point at a page that
does not exist, and corrects itself when either side is republished — none of
which is true of a link someone typed.

It is deliberately not the whole story. A sentence that says "the rule for this
is stated in <page>" carries meaning, and only the writer of that sentence can
put it there; this module makes no such claim and its output says only that two
pages drew on the same material. The prose kind can grow later, on top of this.

What the shape of the data says, measured rather than assumed: the five
published articles share **no** evidence at all — they were accepted as
disjoint problem domains, so nothing here relates them. The overlap lives
between the Docs pages and the articles they distil, where a page's whole
evidence set is often a subset of one article's. Until Docs are published this
produces empty output, which is the honest result and not a bug.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from mem0_local.wiki_check import parse_frontmatter
from mem0_local.wiki_index import NON_ENTRY, write_region

BEGIN = "<!-- related:begin -->"
END = "<!-- related:end -->"

# Both thresholds, not either. A single shared memory is what unrelated pages
# look like — every overlapping pair among the 73 Docs candidates shared
# exactly one — while a ratio alone would promote a two-ref page that happens
# to sit inside a large article. Three refs and a sixth of the smaller side is
# where measured noise stops and measured relation starts.
MIN_SHARED = 3
MIN_SHARE_OF_SMALLER = 0.15


def page_refs(content_dir: Path) -> dict[str, set[str]]:
    """Published pages and the evidence each was written from."""
    out: dict[str, set[str]] = {}
    for page in sorted(content_dir.rglob("*.md")):
        if page.name in NON_ENTRY:
            continue
        front = parse_frontmatter(page.read_text(encoding="utf-8"))
        refs = {entry["ref"] for entry in front.get("sources") or []
                if isinstance(entry, dict) and isinstance(entry.get("ref"), str)}
        if refs:
            out[page.relative_to(content_dir).as_posix()] = refs
    return out


def relations(refs: dict[str, set[str]], *, min_shared: int = MIN_SHARED,
              min_share: float = MIN_SHARE_OF_SMALLER) -> dict[str, list[dict[str, Any]]]:
    """``{page: [related, …]}``, strongest first, symmetric.

    The share is measured against the *smaller* page. A twelve-ref Docs page
    sitting almost entirely inside a five-hundred-ref article is a strong
    relation in one direction and a rounding error in the other; scoring by the
    larger side would hide exactly the case this exists to surface.
    """
    out: dict[str, list[dict[str, Any]]] = {page: [] for page in refs}
    for a, b in itertools.combinations(sorted(refs), 2):
        shared = refs[a] & refs[b]
        if len(shared) < min_shared:
            continue
        smaller = min(len(refs[a]), len(refs[b]))
        share = len(shared) / smaller
        if share < min_share:
            continue
        out[a].append({"page": b, "shared": len(shared), "share": round(share, 3)})
        out[b].append({"page": a, "shared": len(shared), "share": round(share, 3)})
    for page in out:
        out[page].sort(key=lambda r: (-r["shared"], r["page"]))
    return out


def _relative(target: str, source: str) -> str:
    """A link from one page to another, as both Obsidian and GitHub read it."""
    import posixpath

    return posixpath.relpath(target, posixpath.dirname(source)) or target


def render(related: list[dict[str, Any]], source: str, titles: dict[str, str]) -> str:
    if not related:
        return "_No pages share evidence with this one._"
    lines = ["_Pages built from overlapping evidence. Generated; not a claim "
             "that either page says the same thing._", ""]
    for item in related:
        title = titles.get(item["page"], item["page"])
        lines.append(f"- [{title}]({_relative(item['page'], source)})"
                     f" — {item['shared']} shared references")
    return "\n".join(lines) + "\n"


def titles_of(content_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for page in content_dir.rglob("*.md"):
        if page.name in NON_ENTRY:
            continue
        rel = page.relative_to(content_dir).as_posix()
        front = parse_frontmatter(page.read_text(encoding="utf-8"))
        out[rel] = str(front.get("title") or page.stem)
    return out


def build_related(content_dir: Path, **kwargs) -> dict[str, Any]:
    """Write each page's related block. Returns a report."""
    refs = page_refs(content_dir)
    found = relations(refs, **kwargs)
    titles = titles_of(content_dir)
    written: list[str] = []
    for page, items in sorted(found.items()):
        # A page with no relations still gets the block, so that "nothing
        # relates to this" is a stated result rather than a section someone
        # has to wonder about the absence of.
        if write_region(content_dir / page, render(items, page, titles),
                        begin=BEGIN, end=END):
            written.append(page)
    pairs = sum(len(v) for v in found.values()) // 2
    return {
        "pages": len(refs),
        "pairs": pairs,
        "written": written,
        "pages_with_relations": sorted(p for p, v in found.items() if v),
        "min_shared": kwargs.get("min_shared", MIN_SHARED),
        "min_share_of_smaller": kwargs.get("min_share", MIN_SHARE_OF_SMALLER),
    }
