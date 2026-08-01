"""Pages related by the evidence they share, and the noise that is not a relation.

The thresholds are not taste. Among the 73 candidate Docs pages, every pair
that overlapped at all overlapped by exactly one memory — that is what
unrelated pages look like here. Meanwhile a Docs page distilled from an article
often sits entirely inside it. Both facts are load-bearing: one sets the floor,
the other says the share must be measured against the smaller page.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mem0_local.wiki_related import BEGIN, END, build_related, relations, render


def refs(**pages):
    return {name: {f"mem:{r}" for r in spec} for name, spec in pages.items()}


class RelationTest(unittest.TestCase):
    def test_a_single_shared_reference_is_not_a_relation(self):
        found = relations(refs(a="abcdef", b="axyzwv"))
        self.assertEqual(found["a"], [])

    def test_enough_shared_references_make_one(self):
        found = relations(refs(a="abcdef", b="abcxyz"))
        self.assertEqual([r["page"] for r in found["a"]], ["b"])
        self.assertEqual(found["a"][0]["shared"], 3)

    def test_the_share_is_measured_against_the_smaller_page(self):
        # A twelve-ref Docs page inside a five-hundred-ref article is a strong
        # relation one way and a rounding error the other. Scoring by the
        # larger side would hide the case this exists to surface.
        small = set("abcd")
        large = set("abcd") | {f"x{i}" for i in range(200)}
        found = relations({"small": {f"mem:{c}" for c in small},
                           "large": {f"mem:{c}" for c in large}})
        self.assertEqual(found["small"][0]["share"], 1.0)
        self.assertEqual(found["large"][0]["share"], 1.0)

    def test_a_large_overlap_in_absolute_terms_still_needs_the_ratio(self):
        a = {f"mem:{i}" for i in range(100)}
        b = {f"mem:{i}" for i in range(95, 200)}
        self.assertEqual(relations({"a": a, "b": b}), {"a": [], "b": []})

    def test_relations_are_symmetric(self):
        found = relations(refs(a="abcdef", b="abcxyz"))
        self.assertEqual([r["page"] for r in found["b"]], ["a"])

    def test_the_strongest_relation_comes_first(self):
        found = relations(refs(a="abcdefgh", b="abcdefzz", c="abcxxxxx"))
        self.assertEqual([r["page"] for r in found["a"]], ["b", "c"])

    def test_thresholds_are_adjustable_without_touching_the_caller(self):
        self.assertEqual(relations(refs(a="abcdef", b="axyzwv"), min_shared=1,
                                   min_share=0.0)["a"][0]["shared"], 1)


class RenderTest(unittest.TestCase):
    def test_no_relation_says_so_rather_than_leaving_a_gap(self):
        # An absent section reads as an oversight; a stated "nothing" does not.
        self.assertIn("No pages share evidence", render([], "docs/a.md", {}))

    def test_links_are_relative_so_obsidian_and_github_both_resolve_them(self):
        out = render([{"page": "blog/x.md", "shared": 4, "share": 0.4}],
                     "docs/serving/a.md", {"blog/x.md": "X"})
        self.assertIn("(../../blog/x.md)", out)

    def test_the_block_disclaims_what_it_does_not_know(self):
        # Shared evidence is not agreement; the block must not be read as one
        # page endorsing another's claims.
        out = render([{"page": "b.md", "shared": 4, "share": 0.4}], "a.md", {})
        self.assertIn("not a claim", out)


class WriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.content = Path(self._tmp.name) / "content"
        (self.content / "blog").mkdir(parents=True)
        (self.content / "docs").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _page(self, rel, body, *ids):
        entries = "\n".join(f"  - ref: mem:{i}\n    content_hash: h" for i in ids)
        (self.content / rel).write_text(
            f"---\ntitle: {rel}\nsources:\n{entries}\n---\n\n{body}\n", encoding="utf-8")

    def test_the_article_body_is_untouched(self):
        # Blog is a frozen archive: a generated navigation block may sit beside
        # the prose, never inside it.
        body = "The prose that must not move.\n"
        self._page("blog/x.md", body, *"abcdef")
        self._page("docs/y.md", "Docs.", *"abcxyz")
        build_related(self.content)
        after = (self.content / "blog" / "x.md").read_text(encoding="utf-8")
        self.assertIn(body, after)
        self.assertIn(BEGIN, after)
        self.assertLess(after.index(body), after.index(BEGIN))

    def test_regenerating_unchanged_content_writes_nothing(self):
        self._page("blog/x.md", "A.", *"abcdef")
        self._page("docs/y.md", "B.", *"abcxyz")
        build_related(self.content)
        self.assertEqual(build_related(self.content)["written"], [])

    def test_a_page_without_provenance_is_skipped_rather_than_crashing(self):
        (self.content / "docs" / "bare.md").write_text("# Bare\n", encoding="utf-8")
        self._page("blog/x.md", "A.", *"abcdef")
        self.assertEqual(build_related(self.content)["pages"], 1)

    def test_disjoint_pages_report_zero_pairs(self):
        # The five published articles share no evidence at all, so this is the
        # expected result today and not a failure of the computation.
        self._page("blog/x.md", "A.", *"abc")
        self._page("blog/y.md", "B.", *"xyz")
        self.assertEqual(build_related(self.content)["pairs"], 0)


if __name__ == "__main__":
    unittest.main()
