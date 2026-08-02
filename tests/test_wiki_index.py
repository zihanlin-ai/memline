"""The index is generated; everything a human wrote around it is not.

`content/` holds approved, published work and the standing rule is that
compile may only touch a page's `status`. The index generator is the one
sanctioned exception, and the exception is only safe if it is enforced rather
than intended: generated text goes between two markers, and no byte outside
them is ever modified. These tests are what makes that a fact instead of a
comment.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mem0_local.wiki_index import BEGIN, END, build_index, collect, render_entry, write_region


def page(text: str, **front) -> str:
    lines = "\n".join(f"{k}: {v}" for k, v in front.items())
    return f"---\n{lines}\n---\n\n{text}\n"


class IndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.content = Path(self._tmp.name) / "content"
        (self.content / "docs" / "serving").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, rel, text="Body.", **front):
        path = self.content / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page(text, **front), encoding="utf-8")
        return path

    # --- ordering ---------------------------------------------------------

    def test_pages_are_listed_alphabetically_by_title(self):
        self._write("docs/serving/b.md", title="Zebra")
        self._write("docs/serving/a.md", title="Apple")
        self.assertEqual([e["title"] for e in collect(self.content)], ["Apple", "Zebra"])

    def test_an_explicit_order_wins_over_the_alphabet(self):
        # A shelf usually wants its overview read first, and that is judgement
        # the generator cannot make. Stating it is the only way to express it.
        self._write("docs/serving/b.md", title="Zebra", order=1)
        self._write("docs/serving/a.md", title="Apple")
        self.assertEqual([e["title"] for e in collect(self.content)], ["Zebra", "Apple"])

    def test_ordering_is_total_so_output_never_varies(self):
        for name in ("c.md", "a.md", "b.md"):
            self._write(f"docs/serving/{name}", title="Same")
        first = [e["path"] for e in collect(self.content)]
        self.assertEqual(first, sorted(first))

    # --- what a reader needs to choose without opening anything ------------

    def test_the_summary_is_rendered_beside_the_title(self):
        self._write("docs/serving/a.md", title="A", summary="what it is for")
        self.assertIn("— what it is for", render_entry(collect(self.content)[0]))

    def test_a_flagged_page_says_so_in_the_index(self):
        # Status belongs where a reader looks before opening anything: a page
        # whose evidence moved is worth knowing about before it is quoted.
        self._write("docs/serving/a.md", title="A", status="stale-pending-review")
        self.assertIn("`stale-pending-review`", render_entry(collect(self.content)[0]))

    def test_a_page_without_a_title_is_still_listed(self):
        # Missing from the index is missing from retrieval; a bare filename
        # beats an omission.
        self._write("docs/serving/orphan.md")
        self.assertEqual([e["title"] for e in collect(self.content)], ["orphan"])

    def test_readme_and_index_are_not_listed_as_pages(self):
        self._write("docs/serving/README.md", title="Serving")
        self._write("INDEX.md", title="Index")
        self._write("docs/serving/a.md", title="A")
        self.assertEqual([e["title"] for e in collect(self.content)], ["A"])

    # --- the invariant: nothing outside the markers moves ------------------

    def test_human_prose_is_byte_identical_after_regeneration(self):
        readme = self.content / "docs" / "serving" / "README.md"
        prose = "# Serving\n\nReusable knowledge from the serving programme.\n"
        readme.write_text(prose + f"\n{BEGIN}\nold\n{END}\n", encoding="utf-8")
        self._write("docs/serving/a.md", title="A")
        build_index(self.content)
        after = readme.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(prose), "the paragraph above the markers changed")
        self.assertNotIn("old", after)

    def test_a_file_without_markers_is_only_appended_to(self):
        readme = self.content / "docs" / "serving" / "README.md"
        prose = "# Serving\n\nWhat this shelf is for.\n"
        readme.write_text(prose, encoding="utf-8")
        before = hashlib.sha256(prose.encode()).hexdigest()
        self._write("docs/serving/a.md", title="A")
        build_index(self.content)
        after = readme.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(prose))
        self.assertEqual(hashlib.sha256(after[:len(prose)].encode()).hexdigest(), before)

    def test_only_the_region_between_markers_is_replaced(self):
        path = self.content / "x.md"
        path.write_text(f"before\n{BEGIN}\nstale\n{END}\nafter\n", encoding="utf-8")
        write_region(path, "fresh")
        self.assertEqual(path.read_text(encoding="utf-8"),
                         f"before\n{BEGIN}\nfresh\n{END}\nafter\n")

    def test_rewriting_unchanged_content_touches_nothing(self):
        # Idempotence is what makes it safe to run on every publish: a run
        # that rewrites files it did not change produces diff noise, and diff
        # noise is how a real change gets waved through.
        self._write("docs/serving/a.md", title="A", summary="s")
        build_index(self.content)
        self.assertEqual(build_index(self.content)["written"], [])

    # --- reporting rather than blocking -----------------------------------

    def test_pages_without_a_summary_are_named(self):
        self._write("docs/serving/a.md", title="A")
        self._write("docs/serving/b.md", title="B", summary="has one")
        self.assertEqual(build_index(self.content)["pages_without_summary"],
                         ["docs/serving/a.md"])

    def test_pages_without_a_topic_key_are_named(self):
        self._write("docs/serving/a.md", title="A", summary="has one")
        self._write("docs/serving/b.md", title="B", summary="has one", topic_key="b")
        self.assertEqual(build_index(self.content)["pages_without_topic_key"],
                         ["docs/serving/a.md"])

    def test_an_empty_wiki_produces_an_index_that_says_so(self):
        report = build_index(self.content)
        self.assertEqual(report["pages"], 0)
        self.assertIn("No pages published yet", (self.content / "INDEX.md").read_text())



class StatusNoiseTest(unittest.TestCase):
    """A marker that appears on every line is read on none of them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.content = Path(self._tmp.name) / "content"
        (self.content / "blog").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, rel, **front):
        (self.content / rel).write_text(page("Body.", **front), encoding="utf-8")

    def test_a_healthy_page_shows_no_status(self):
        # Every published page carries `status: published`; printing it beside
        # each of them was the generator's first output against real content.
        self._write("blog/a.md", title="A", status="published")
        self.assertNotIn("`published`", render_entry(collect(self.content)[0]))

    def test_an_unhealthy_page_still_shows_its_status(self):
        self._write("blog/a.md", title="A", status="stale-pending-review")
        self.assertIn("`stale-pending-review`", render_entry(collect(self.content)[0]))

    def test_the_report_counts_only_the_unhealthy_ones(self):
        self._write("blog/a.md", title="A", status="published")
        self._write("blog/b.md", title="B", status="stale-pending-review")
        self.assertEqual([f["path"] for f in build_index(self.content)["flagged_pages"]],
                         ["blog/b.md"])
if __name__ == "__main__":
    unittest.main()
