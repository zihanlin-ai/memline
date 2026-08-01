"""Unit tests for the wiki provenance/link checker (pure functions, fake store).

Covers the review-mandated cases: supersession detection over the real
``{"heads": [...]}`` payload shape, missing content hashes, empty provenance,
resolve_head failure reporting, and body-link integrity.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mem0_local.wiki_check import run_check, sha256_text

MEM_ID = "11111111-1111-1111-1111-111111111111"
HEAD_ID = "22222222-2222-2222-2222-222222222222"
MEM_TEXT = "a fact"


def make_execute(*, head=None, get_raises=False, head_raises=False):
    def execute(op, args):
        if op == "get":
            if get_raises:
                raise RuntimeError("not found")
            return {"memory": MEM_TEXT}
        if op == "resolve_head":
            if head_raises:
                raise RuntimeError("daemon down")
            return head
        raise AssertionError(f"unexpected op {op}")

    return execute


class WikiCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "content").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def page(self, body, name="page.md"):
        path = self.root / "content" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def flags(self, execute):
        return run_check(self.root, execute)["flags"]

    def provenance_page(self, content_hash=None, *, name="page.md"):
        hash_line = f'    content_hash: "{content_hash}"\n' if content_hash else ""
        self.page(
            "---\nsources:\n"
            + f'  - ref: "mem:{MEM_ID}"\n'
            + hash_line
            + "---\nbody\n",
            name=name,
        )

    def kinds(self, execute):
        return [f["kind"] for f in self.flags(execute)]

    def test_docs_require_an_active_memory_head(self):
        self.provenance_page(sha256_text(MEM_TEXT), name="docs/page.md")
        kinds = self.kinds(make_execute(head={"heads": [HEAD_ID]}))
        self.assertEqual(kinds, ["memory_superseded"])

    def test_docs_with_an_active_memory_head_are_clean(self):
        self.provenance_page(sha256_text(MEM_TEXT), name="docs/page.md")
        kinds = self.kinds(make_execute(head={"heads": [MEM_ID]}))
        self.assertEqual(kinds, [])

    def test_frozen_blog_may_archive_a_superseded_memory(self):
        self.provenance_page(sha256_text(MEM_TEXT), name="blog/post.md")
        kinds = self.kinds(make_execute(head={"heads": [HEAD_ID]}))
        self.assertEqual(kinds, [])

    def test_blog_archive_still_checks_publication_time_content_hash(self):
        self.provenance_page(sha256_text("older text"), name="blog/post.md")
        kinds = self.kinds(make_execute(head={"heads": [HEAD_ID]}))
        self.assertEqual(kinds, ["memory_content_changed"])

    def test_missing_content_hash_is_flagged(self):
        self.provenance_page(content_hash=None)
        self.assertIn("missing_content_hash", self.kinds(make_execute(head=None)))

    def test_empty_sources_list_counts_as_missing_provenance(self):
        self.page("---\nsources: []\n---\nbody\n")
        self.assertEqual(self.kinds(make_execute(head=None)), ["missing_provenance"])

    def test_resolve_head_failure_is_reported_not_swallowed(self):
        self.provenance_page(sha256_text(MEM_TEXT))
        kinds = self.kinds(make_execute(head_raises=True))
        self.assertEqual(kinds, ["memory_status_unverified"])

    def test_changed_memory_text_is_flagged(self):
        self.provenance_page(sha256_text("older text"))
        self.assertIn("memory_content_changed", self.kinds(make_execute(head=None)))

    def test_missing_memory_is_flagged(self):
        self.provenance_page(sha256_text(MEM_TEXT))
        self.assertEqual(self.kinds(make_execute(get_raises=True)), ["memory_missing"])

    def test_source_section_ref_checks_the_section_hash(self):
        sources = self.root / "sources"
        sources.mkdir()
        (sources / "runbook.md").write_text(
            "# Runbook\n\n## First\n\none\n\n## Second\n\ntwo\n", encoding="utf-8"
        )
        section = "## Second\n\ntwo"
        self.page(
            "---\nsources:\n"
            + '  - ref: "sources/runbook.md#Second"\n'
            + f'    content_hash: "{sha256_text(section)}"\n'
            + "---\nbody\n"
        )
        self.assertEqual(self.kinds(make_execute(head=None)), [])

    def test_missing_source_section_is_flagged(self):
        sources = self.root / "sources"
        sources.mkdir()
        (sources / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
        self.page(
            "---\nsources:\n"
            + '  - ref: "sources/runbook.md#Missing"\n'
            + f'    content_hash: "{sha256_text("## Missing")}"\n'
            + "---\nbody\n"
        )
        self.assertEqual(
            self.kinds(make_execute(head=None)), ["source_section_missing"]
        )

    def test_broken_relative_link_is_flagged(self):
        self.page(
            "---\nsources:\n"
            + f'  - ref: "mem:{MEM_ID}"\n'
            + f'    content_hash: "{sha256_text(MEM_TEXT)}"\n'
            + "---\nsee [gone](./gone.md)\n"
        )
        self.assertIn("link_broken", self.kinds(make_execute(head=None)))

    def test_readme_pages_are_exempt_from_provenance(self):
        self.page("# index\n", name="README.md")
        self.assertEqual(self.kinds(make_execute(head=None)), [])


if __name__ == "__main__":
    unittest.main()
