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

from memline.wiki_index import BEGIN, END, build_index, collect, refresh, render_entry


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

    def test_a_readme_is_not_listed_as_a_page(self):
        self._write("docs/serving/README.md", title="Serving")
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

    def test_a_hand_written_readme_without_markers_is_left_alone(self):
        # Someone wrote a landing page and did not ask for a list of children
        # under it. Appending one anyway is how every directory ended up with a
        # generated link table nobody chose.
        readme = self.content / "docs" / "serving" / "README.md"
        prose = "# Serving\n\nWhat this shelf is for.\n"
        readme.write_text(prose, encoding="utf-8")
        before = hashlib.sha256(prose.encode()).hexdigest()
        self._write("docs/serving/a.md", title="A")
        build_index(self.content)
        after = readme.read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(after.encode()).hexdigest(), before)

    # --- the listing is opt-in --------------------------------------------

    def test_a_shelf_without_a_readme_does_not_get_one(self):
        # Most directories should have no landing page at all. The generator
        # cannot judge which ones deserve it, so it never decides.
        self._write("docs/serving/a.md", title="A")
        report = build_index(self.content)
        self.assertFalse((self.content / "docs" / "serving" / "README.md").exists())
        self.assertEqual(report["shelves_listed"], [])

    def test_a_readme_with_markers_still_gets_its_listing(self):
        readme = self.content / "docs" / "serving" / "README.md"
        readme.write_text(f"# Serving\n\nOrientation.\n\n{BEGIN}\nstale\n{END}\n",
                          encoding="utf-8")
        self._write("docs/serving/a.md", title="A", summary="what it is for")
        report = build_index(self.content)
        body = readme.read_text(encoding="utf-8")
        self.assertIn("[A](a.md) — what it is for", body)
        self.assertNotIn("stale", body)
        self.assertEqual(report["shelves_listed"], ["docs/serving"])

    def test_removing_the_markers_retires_the_listing_for_good(self):
        # The reason deletion sticks: a shelf that opts out stays opted out,
        # so cleaning up a generated README is not undone by the next publish.
        readme = self.content / "docs" / "serving" / "README.md"
        readme.write_text(f"{BEGIN}\nold\n{END}\n", encoding="utf-8")
        self._write("docs/serving/a.md", title="A")
        build_index(self.content)
        readme.unlink()
        build_index(self.content)
        self.assertFalse(readme.exists())

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

    def test_an_empty_wiki_writes_nothing(self):
        report = build_index(self.content)
        self.assertEqual(report["pages"], 0)
        self.assertEqual(report["written"], [])
        self.assertEqual(list(self.content.rglob("*.md")), [])

    def test_no_whole_corpus_index_is_produced(self):
        # Retired deliberately: a file naming every page matched almost every
        # query and answered none of them. Routing is docs/.nav.yml now.
        self._write("docs/serving/a.md", title="A", summary="s")
        build_index(self.content)
        self.assertFalse((self.content / "INDEX.md").exists())

class RefreshTest(unittest.TestCase):
    """One entry point for everything computed, because half of it is wrong.

    Listings and relation blocks are both derived from what the pages declare
    and both are only correct right after a publish. Running one without the
    other leaves the wiki in a state neither intended, so the publisher should
    not have to remember a list.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.content = Path(self._tmp.name) / "content"
        (self.content / "docs").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _page(self, rel, refs=(), **front):
        path = self.content / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        src = "".join(f"  - ref: {r}\n" for r in refs)
        lines = "\n".join(f"{k}: {v}" for k, v in front.items())
        body = f"---\n{lines}\n" + (f"sources:\n{src}" if src else "") + "---\n\nBody.\n"
        path.write_text(body, encoding="utf-8")

    def test_one_call_writes_both_kinds_of_block(self):
        shared = ["mem:a", "mem:b", "mem:c"]
        self._page("docs/a.md", shared, title="A", summary="s", topic_key="a")
        self._page("docs/b.md", shared, title="B", summary="s", topic_key="b")
        (self.content / "docs" / "README.md").write_text(
            f"# Docs\n\n{BEGIN}\nstale\n{END}\n", encoding="utf-8")
        report = refresh(self.content)
        readme = (self.content / "docs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("[A](a.md)", readme)
        self.assertNotIn("stale", readme)
        self.assertIn("related:begin", (self.content / "docs" / "a.md").read_text())
        self.assertEqual(report["relation_pairs"], 1)

    def test_the_report_names_every_file_it_touched(self):
        # Both kinds of write land in one list, so a publisher reviewing the
        # diff has one place to look rather than two reports to reconcile.
        self._page("docs/a.md", ["mem:a"], title="A", summary="s", topic_key="a")
        (self.content / "docs" / "README.md").write_text(
            f"# Docs\n\n{BEGIN}\n{END}\n", encoding="utf-8")
        written = refresh(self.content)["written"]
        self.assertIn("docs/README.md", written)
        self.assertIn("docs/a.md", written)

    def test_a_page_with_no_relations_still_says_so(self):
        self._page("docs/a.md", ["mem:a"], title="A", summary="s", topic_key="a")
        refresh(self.content)
        self.assertIn("related:begin", (self.content / "docs" / "a.md").read_text())

    def test_refreshing_unchanged_content_touches_nothing(self):
        self._page("docs/a.md", ["mem:a"], title="A", summary="s", topic_key="a")
        refresh(self.content)
        self.assertEqual(refresh(self.content)["written"], [])
