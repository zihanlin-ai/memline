"""The skeleton is written by a person; the drift from it is found by a program.

`docs/.nav.yml` encodes routing judgement — entry points and reading order —
which is why nothing generates it. These tests hold the other half of that
bargain: the checker must notice when a published page is routed to by nothing,
and when an entry survives a rename that moved the page out from under it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memline.wiki_nav import check_nav


class NavCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.docs = Path(self._tmp.name) / "docs"
        self.docs.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _page(self, rel: str):
        path = self.docs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Page\n", encoding="utf-8")

    def _nav(self, body: str):
        (self.docs / ".nav.yml").write_text(body, encoding="utf-8")

    def test_a_named_page_is_reached(self):
        self._page("serving/observability.md")
        self._nav("nav:\n  - Serving:\n      - Observe: serving/observability.md\n")
        self.assertTrue(check_nav(self.docs)["clean"])

    def test_a_glob_reaches_the_rest_of_a_directory(self):
        # Globs are what keep a newly published page from being invisible
        # before anyone remembers to route to it by name.
        self._page("serving/a.md")
        self._page("serving/b.md")
        self._nav("nav:\n  - Serving:\n      - serving/*\n")
        self.assertEqual(check_nav(self.docs)["unreachable"], [])

    def test_naming_a_directory_reaches_its_pages(self):
        self._page("features/speculative_decoding/mtp.md")
        self._nav("nav:\n  - Features:\n      - Spec: features/speculative_decoding\n")
        self.assertEqual(check_nav(self.docs)["unreachable"], [])

    def test_a_page_no_entry_reaches_is_reported(self):
        self._page("serving/observability.md")
        self._page("serving/orphan.md")
        self._nav("nav:\n  - Serving:\n      - Observe: serving/observability.md\n")
        report = check_nav(self.docs)
        self.assertEqual(report["unreachable"], ["serving/orphan.md"])
        self.assertFalse(report["clean"])

    def test_an_entry_pointing_nowhere_is_reported(self):
        # The signature of a rename the skeleton did not follow.
        self._page("serving/proxy_readiness.md")
        self._nav("nav:\n  - Serving:\n      - Readiness: serving/proxy/readiness.md\n"
                  "      - Actual: serving/proxy_readiness.md\n")
        report = check_nav(self.docs)
        self.assertEqual(report["dangling"], ["serving/proxy/readiness.md"])

    def test_a_readme_is_never_called_unreachable(self):
        # Routing to a chapter is routing to its landing page; a README is
        # reached by the directory that holds it.
        self._page("features/README.md")
        self._page("features/eplb.md")
        self._nav("nav:\n  - Features:\n      - EPLB: features/eplb.md\n")
        self.assertEqual(check_nav(self.docs)["unreachable"], [])

    def test_external_links_are_not_treated_as_pages(self):
        self._page("serving/a.md")
        self._nav("nav:\n  - Serving:\n      - Upstream: https://docs.vllm.ai/\n"
                  "      - A: serving/a.md\n")
        self.assertEqual(check_nav(self.docs)["dangling"], [])

    def test_a_missing_skeleton_is_reported_not_raised(self):
        self._page("serving/a.md")
        report = check_nav(self.docs)
        self.assertFalse(report["present"])
        self.assertFalse(report["clean"])

    def test_the_checker_writes_nothing(self):
        self._page("serving/a.md")
        self._nav("nav:\n  - Serving:\n      - A: serving/a.md\n")
        before = {p: p.read_bytes() for p in self.docs.rglob("*") if p.is_file()}
        check_nav(self.docs)
        after = {p: p.read_bytes() for p in self.docs.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
