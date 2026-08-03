"""The page-format primitives every wiki module builds on.

The region contract carries the whole safety story of generated content: a
generator may rewrite what lies between its markers and nothing else, and it
may not create a file — creating one is a publishing decision. These tests
held that contract in the index generator's suite before the primitive moved
here; they moved with it because the contract belongs to the format, not to
any one generator.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memline.wiki.page import (
    INDEX_BEGIN,
    INDEX_END,
    RELATED_BEGIN,
    RELATED_END,
    parse_frontmatter,
    write_region,
)


class WriteRegionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_only_the_region_between_markers_is_replaced(self):
        path = self.dir / "x.md"
        path.write_text(f"before\n{INDEX_BEGIN}\nstale\n{INDEX_END}\nafter\n",
                        encoding="utf-8")
        write_region(path, "fresh", begin=INDEX_BEGIN, end=INDEX_END)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"before\n{INDEX_BEGIN}\nfresh\n{INDEX_END}\nafter\n")

    def test_a_missing_file_is_not_created(self):
        # The create path is how every directory once acquired a README.
        missing = self.dir / "nothing.md"
        self.assertFalse(write_region(missing, "body",
                                      begin=INDEX_BEGIN, end=INDEX_END))
        self.assertFalse(missing.exists())

    def test_each_region_is_rewritten_without_disturbing_the_other(self):
        # The reason there are two marker pairs at all.
        path = self.dir / "x.md"
        path.write_text(f"{INDEX_BEGIN}\nlist\n{INDEX_END}\n\n"
                        f"{RELATED_BEGIN}\nlinks\n{RELATED_END}\n", encoding="utf-8")
        write_region(path, "new links", begin=RELATED_BEGIN, end=RELATED_END)
        text = path.read_text(encoding="utf-8")
        self.assertIn("list", text)
        self.assertIn("new links", text)
        self.assertNotIn("\nlinks\n", text)

    def test_an_unchanged_rewrite_touches_nothing(self):
        path = self.dir / "x.md"
        path.write_text(f"{INDEX_BEGIN}\nsame\n{INDEX_END}\n", encoding="utf-8")
        self.assertFalse(write_region(path, "same", begin=INDEX_BEGIN, end=INDEX_END))


class FrontmatterTest(unittest.TestCase):
    def test_a_page_without_frontmatter_reads_as_empty(self):
        self.assertEqual(parse_frontmatter("# Just a title\n"), {})

    def test_broken_yaml_reads_as_empty_rather_than_raising(self):
        # A malformed page is the checker's finding to report, not this
        # parser's crash to raise.
        self.assertEqual(parse_frontmatter("---\n: : :\n---\nbody\n"), {})

    def test_fields_come_back_as_written(self):
        page = "---\ntitle: T\norder: 2\n---\nbody\n"
        self.assertEqual(parse_frontmatter(page), {"title": "T", "order": 2})


if __name__ == "__main__":
    unittest.main()
