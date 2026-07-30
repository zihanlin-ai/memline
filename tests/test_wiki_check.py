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
        (self.root / "content" / name).write_text(body, encoding="utf-8")

    def flags(self, execute):
        return run_check(self.root, execute)["flags"]

    def provenance_page(self, content_hash=None):
        hash_line = f'    content_hash: "{content_hash}"\n' if content_hash else ""
        self.page(
            "---\nsources:\n" + f'  - ref: "mem:{MEM_ID}"\n' + hash_line + "---\nbody\n"
        )

    def kinds(self, execute):
        return [f["kind"] for f in self.flags(execute)]

    def test_superseded_head_list_of_strings_is_detected(self):
        self.provenance_page(sha256_text(MEM_TEXT))
        kinds = self.kinds(make_execute(head={"heads": [HEAD_ID]}))
        self.assertEqual(kinds, ["memory_superseded"])

    def test_active_head_is_clean(self):
        self.provenance_page(sha256_text(MEM_TEXT))
        kinds = self.kinds(make_execute(head={"heads": [MEM_ID]}))
        self.assertEqual(kinds, [])

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

    def test_broken_relative_link_is_flagged(self):
        self.page("---\nsources:\n" + f'  - ref: "mem:{MEM_ID}"\n'
                  + f'    content_hash: "{sha256_text(MEM_TEXT)}"\n'
                  + "---\nsee [gone](./gone.md)\n")
        self.assertIn("link_broken", self.kinds(make_execute(head=None)))

    def test_readme_pages_are_exempt_from_provenance(self):
        self.page("# index\n", name="README.md")
        self.assertEqual(self.kinds(make_execute(head=None)), [])


if __name__ == "__main__":
    unittest.main()
