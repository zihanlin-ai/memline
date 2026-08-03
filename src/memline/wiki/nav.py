"""Check the hand-written navigation skeleton against the pages that exist.

``docs/.nav.yml`` is deliberately not generated. It carries the judgement a
directory listing cannot express — which page is an entry point, what order a
group is read in — and that judgement has to come from a person. What a program
*can* do is notice when the skeleton and the corpus have drifted apart: a page
published into a chapter nobody routed to, or an entry still pointing at a page
that was renamed.

So this module never writes. It answers two questions and stops:

- **unreachable** — pages no entry, named or globbed, can reach. A page that
  cannot be reached from the skeleton is one an agent will only find by luck.
- **dangling** — entries that resolve to nothing. Usually a rename that the
  skeleton did not follow.

Both are reported rather than fixed, because the fix is a routing decision:
where the page belongs and what it should be read after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

NAV_FILE = ".nav.yml"


def _targets(node: Any) -> list[str]:
    """Every path-shaped leaf in the skeleton, whatever nesting holds it.

    The file's shape is for human reading — a group may be a list, a mapping of
    label to target, or a mapping of label to a nested list. None of that
    changes which paths were named, so the walk flattens it all and keeps the
    strings that look like a target rather than prose.
    """
    found: list[str] = []
    if isinstance(node, str):
        if not node.startswith(("http://", "https://")):
            found.append(node)
    elif isinstance(node, list):
        for item in node:
            found += _targets(item)
    elif isinstance(node, dict):
        for value in node.values():
            found += _targets(value)
    return found


def _resolve(docs: Path, target: str) -> list[Path]:
    """The pages an entry reaches: a file, every page under a directory, or a glob."""
    if "*" in target:
        return sorted(p for p in docs.glob(target) if p.suffix == ".md")
    path = docs / target
    if path.is_dir():
        return sorted(path.rglob("*.md"))
    return [path] if path.is_file() else []


def check_nav(docs_dir: Path) -> dict[str, Any]:
    """Compare the skeleton with the pages on disk. Reports; never writes."""
    import yaml

    nav_path = docs_dir / NAV_FILE
    if not nav_path.is_file():
        return {"nav_file": str(nav_path), "present": False, "clean": False,
                "reason": "no navigation skeleton", "unreachable": [], "dangling": []}

    skeleton = yaml.safe_load(nav_path.read_text(encoding="utf-8")) or {}
    entries = _targets(skeleton.get("nav", skeleton))

    reached: set[Path] = set()
    dangling: list[str] = []
    for entry in entries:
        hit = _resolve(docs_dir, entry)
        if hit:
            reached.update(hit)
        else:
            dangling.append(entry)

    # A README is a landing page for the directory that holds it, so it is
    # reached by definition — routing to a chapter is routing to its README.
    pages = {p for p in docs_dir.rglob("*.md") if p.name != "README.md"}
    unreachable = sorted(str(p.relative_to(docs_dir)) for p in pages - reached)

    return {
        "nav_file": str(nav_path),
        "present": True,
        "entries": len(entries),
        "pages": len(pages),
        "unreachable": unreachable,
        "dangling": sorted(dangling),
        "clean": not unreachable and not dangling,
    }
