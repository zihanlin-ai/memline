"""A dropped investigation and a dropped stray are not the same omission.

Coverage as a ratio cannot tell them apart: forty scattered memories and one
whole thread of forty weigh identically. These tests pin the distinction the
thread view exists to make.
"""

from __future__ import annotations

import unittest

from mem0_local.wiki_threads import cited_refs, normalize_evidence, thread_usage


def thread(key, ids, **extra):
    return {"thread_key": key, "evidence_ids": list(ids), "what": f"work on {key}", **extra}


class NormalizeTest(unittest.TestCase):
    def test_bare_memory_ids_gain_the_mem_prefix(self):
        # Profiles name memories bare; everything downstream says mem:<id>.
        # Without this the intersection is empty and every thread reads as
        # dropped — a bug that looks exactly like a finding.
        self.assertEqual(normalize_evidence(thread("t", ["abc-123"])), {"mem:abc-123"})

    def test_prefixed_refs_are_left_alone(self):
        self.assertEqual(
            normalize_evidence(thread("t", ["mem:abc", "sources/doc.md#H"])),
            {"mem:abc", "sources/doc.md#H"})

    def test_empty_and_non_string_members_are_skipped(self):
        self.assertEqual(normalize_evidence(thread("t", ["", None, 7, "x"])), {"mem:x"})


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.threads = {
            ("b0", "kept"): thread("kept", ["a", "b", "c"]),
            ("b0", "one-citation"): thread("one-citation", ["d", "e", "f", "g"]),
            ("b0", "dropped-big"): thread("dropped-big", ["h", "i", "j", "k", "l"]),
            ("b0", "dropped-small"): thread("dropped-small", ["m"]),
            ("b1", "other-topic"): thread("other-topic", ["y", "z"]),
        }
        self.approved = {f"mem:{c}" for c in "abcdefghijklm"}

    def _usage(self, cited_letters):
        return thread_usage(self.threads, self.approved,
                            {f"mem:{c}" for c in cited_letters})

    def test_threads_outside_the_topic_are_not_counted_as_dropped(self):
        # The store holds hundreds of threads this article was never meant to
        # tell. Counting them would bury the ones that were.
        report = self._usage("abcd")
        self.assertEqual(report["contributing_threads"], 4)
        self.assertNotIn("other-topic", [t["thread_key"] for t in report["dropped"]])

    def test_a_single_citation_keeps_a_thread_off_the_dropped_list(self):
        report = self._usage("abcd")
        self.assertEqual([t["thread_key"] for t in report["dropped"]],
                         ["dropped-big", "dropped-small"])

    def test_dropped_threads_are_ordered_by_what_they_carried(self):
        self.assertEqual([t["members"] for t in self._usage("abcd")["dropped"]], [5, 1])

    def test_share_counts_evidence_not_threads(self):
        # Two dropped threads out of four, but six refs of thirteen: the
        # thread count alone would understate what went missing.
        report = self._usage("abcd")
        self.assertEqual(report["evidence_in_dropped_threads"], 6)
        self.assertEqual(report["share_of_evidence_dropped_whole"], round(6 / 13, 3))

    def test_a_thin_survivor_is_still_reported(self):
        # One citation out of four is not a dropped thread, but it is the same
        # problem wearing a smaller number, so it stays visible.
        thin = [t for t in self._usage("abcd")["threads"] if t["thread_key"] == "one-citation"]
        self.assertEqual((thin[0]["cited"], thin[0]["members"]), (1, 4))

    def test_nothing_cited_drops_every_contributing_thread(self):
        report = self._usage("")
        self.assertEqual(report["dropped_threads"], report["contributing_threads"])
        self.assertEqual(report["share_of_evidence_dropped_whole"], 1.0)

    def test_no_threshold_is_applied(self):
        # Deliberately absent: most material does not deserve writing down, so
        # a low share is not a failure and this report never says it is.
        self.assertNotIn("clean", self._usage("abcd"))


class CitationTest(unittest.TestCase):
    def test_only_footnote_citations_count(self):
        md = "Cited.^[mem:aaaa1111-2222] A bare [mem:bbbb2222-3333] is not a citation."
        self.assertEqual(cited_refs(md), {"mem:aaaa1111-2222"})


if __name__ == "__main__":
    unittest.main()
