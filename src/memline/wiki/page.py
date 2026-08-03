"""What every wiki module agrees a page *is*. Nothing here judges or writes prose.

This layer exists because the page format had no home of its own. Frontmatter
parsing lived in the checker, the citation regex lived in the draft gate, the
generated-region markers lived in the index generator — and every module that
needed one of them reached into whichever tool happened to define it. That put
checkers underneath generators in the import graph (a checker is supposed to
sit on top, looking down) and produced a genuine cycle: the index imported the
related-block builder for its refresh, while the related-block builder
imported the index for ``write_region``. Both were borrowing page format,
not each other's behaviour.

So the format facts live here, and only format facts:

* a page's frontmatter and how to read it;
* the citation token an article uses to ground a sentence;
* the marker pairs that delimit a page's two generated regions, and the one
  function allowed to rewrite what is between them;
* which filenames describe a shelf rather than belonging to it;
* the hash that provenance records are kept in.

The one duplication left standing is deliberate: ``bundle.sha256_text`` is the
same three lines, and stays, because bundle sits *below* the wiki family — an
outbound-sanitization layer must not import page format to hash a string, and
an arrow from there up to here would be the same backwards edge this module
was created to remove.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

# The citation token: how a published sentence names its evidence. Accepts a
# memory reference or a designated source section. The draft gate keeps its
# own wider variants (a malformed token is *its* business, not a page fact).
CITATION = re.compile(r"\^\[(mem:[0-9a-fA-F-]{8,}|sources/[^\]]+)\]")

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# A README describes the shelf that holds it rather than belonging to it.
NON_ENTRY = {"README.md"}

# A page may carry two generated regions, and these markers are the contract:
# a generator may rewrite what lies between its pair and must not touch one
# byte outside it. The pairs are distinct so each region can be rewritten
# without disturbing the other.
INDEX_BEGIN = "<!-- index:begin -->"
INDEX_END = "<!-- index:end -->"
RELATED_BEGIN = "<!-- related:begin -->"
RELATED_END = "<!-- related:end -->"


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


def region_pattern(begin: str, end: str) -> re.Pattern[str]:
    return re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)


def write_region(path: Path, body: str, *, begin: str, end: str) -> bool:
    """Replace the marked region in ``path``. Returns whether anything changed.

    Everything outside the markers is preserved exactly. A file without markers
    gains the block at the end — appended, never merged into what is there.

    A file that does not exist is left that way and the call reports no change.
    This function writes into approved, published work; creating a file there
    is a publishing decision, and it is not one a generator gets to make. The
    directory-wide READMEs this wiki had to delete all came from a create path
    exactly like the one that used to live here.
    """
    if not path.is_file():
        return False
    pattern = region_pattern(begin, end)
    block = f"{begin}\n{body.rstrip()}\n{end}"
    current = path.read_text(encoding="utf-8")
    if pattern.search(current):
        updated = pattern.sub(lambda _: block, current, count=1)
    else:
        updated = current.rstrip("\n") + "\n\n" + block + "\n"
    if updated == current:
        return False
    path.write_text(updated, encoding="utf-8")
    return True
