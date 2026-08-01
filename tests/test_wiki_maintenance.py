"""Retired evidence becomes a suggestion, or it becomes nothing at all.

A superseded memory is at least visible from the store: its session moved, so
the next incremental compile re-reads and re-profiles it. A deleted one is
invisible — the plan iterates memories that exist, and one that is gone leaves
nothing to iterate. The published page's frontmatter is the only surviving
record that anything ever depended on it, which is why the page check has to
run inside compile rather than beside it.
"""

from __future__ import annotations

import unittest

from mem0_local.wiki_suggest import maintenance_suggestions


def report(*flags):
    return {"flags": list(flags), "flag_count": len(flags)}


class MaintenanceSuggestionTest(unittest.TestCase):
    def test_a_deleted_memory_becomes_a_suggestion(self):
        out = maintenance_suggestions(
            report({"page": "content/docs/a/b.md", "ref": "mem:1", "kind": "memory_missing"}),
            run=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "maintenance")
        self.assertEqual(out[0]["affected_pages"], "content/docs/a/b.md")
        self.assertIn("no longer exists", out[0]["value"])

    def test_a_frozen_blog_gets_an_erratum_not_an_edit(self):
        # Rewriting the body would erase the belief the article exists to
        # record, so the correction goes beside it.
        out = maintenance_suggestions(
            report({"page": "content/blog/x.md", "ref": "mem:1", "kind": "memory_superseded"}),
            run=2)
        self.assertEqual(out[0]["type"], "erratum")
        self.assertIn("errata", out[0]["rationale"])

    def test_a_changed_memory_is_distinguished_from_a_missing_one(self):
        # The dangerous case: the citation still resolves, so nothing looks
        # broken, but the text behind it no longer says what the page claims.
        out = maintenance_suggestions(
            report({"page": "content/docs/a.md", "ref": "mem:1",
                    "kind": "memory_content_changed"}), run=2)
        self.assertEqual(out[0]["flag_categories"], ["changed-evidence"])
        self.assertIn("may no longer say what the page claims", out[0]["value"])

    def test_one_page_with_many_dead_citations_is_one_decision(self):
        out = maintenance_suggestions(
            report(*[{"page": "content/docs/a.md", "ref": f"mem:{i}", "kind": "memory_missing"}
                     for i in range(11)]), run=2)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["evidence"]), 11)
        self.assertIn("11 flag(s)", out[0]["evidence_gaps"])

    def test_ids_continue_the_run_rather_than_restarting_it(self):
        out = maintenance_suggestions(
            report({"page": "content/docs/a.md", "kind": "missing_provenance"}),
            run=7, start_number=73)
        self.assertEqual(out[0]["id"], "run-007-S074")

    def test_pages_are_reported_in_a_stable_order(self):
        out = maintenance_suggestions(
            report({"page": "content/docs/z.md", "kind": "link_broken"},
                   {"page": "content/docs/a.md", "kind": "link_broken"}), run=1)
        self.assertEqual([s["affected_pages"] for s in out],
                         ["content/docs/a.md", "content/docs/z.md"])

    def test_a_clean_check_proposes_nothing(self):
        self.assertEqual(maintenance_suggestions(report(), run=1), [])

    def test_a_flag_without_a_page_is_ignored_rather_than_crashing(self):
        self.assertEqual(maintenance_suggestions(report({"kind": "memory_missing"}), run=1), [])


if __name__ == "__main__":
    unittest.main()
