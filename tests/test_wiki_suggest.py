"""Assembling associations into suggestions.

The judgement is the agent's; what is tested here is that the bookkeeping
around it cannot quietly lose evidence, resurrect a rejected topic, or hide a
thread key that does not exist.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mem0_local.wiki_suggest import build_suggestions, load_threads, suppressed_topics

TEXTS = {"m1": "first fact", "m2": "second fact", "m3": "third fact"}


def resolve(memory_id):
    return TEXTS.get(memory_id)


def profile(batch_id, threads, session="s1"):
    return {"batch_id": batch_id, "status": "ok",
            "batch": {"kind": "session", "span": ["2026-07-01", "2026-07-02"]},
            "profile": {"sessions": [{"session_id": session, "span": "2026-07-01..02",
                                      "threads": threads}]}}


def thread(key, ids):
    return {"thread_key": key, "what": "x", "outcome": "y", "evidence_ids": ids}


class SuggestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "b000.json").write_text(json.dumps(
            profile("b000", [thread("t1", ["m1", "m2"])])))
        (self.dir / "b001.json").write_text(json.dumps(
            profile("b001", [thread("t2", ["m2", "m3"])], session="s2")))
        self.threads = load_threads(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def assoc(self, members, **kw):
        return [{"topic_key": "k", "type": "blog", "title": "T", "value": "v",
                 "members": members, **kw}]

    def test_evidence_is_unioned_across_members_without_duplicates(self):
        s, _ = build_suggestions(
            self.assoc([{"batch_id": "b000", "thread_key": "t1"},
                        {"batch_id": "b001", "thread_key": "t2"}]),
            self.threads, resolve, run=1)
        refs = [e["ref"] for e in s[0]["evidence"]]
        self.assertEqual(refs, ["mem:m1", "mem:m2", "mem:m3"])

    def test_evidence_hash_is_of_current_text(self):
        s, _ = build_suggestions(self.assoc([{"batch_id": "b000", "thread_key": "t1"}]),
                                 self.threads, resolve, run=1)
        self.assertEqual(s[0]["evidence"][0]["sha256"],
                         hashlib.sha256(b"first fact").hexdigest())

    def test_a_thread_key_that_does_not_exist_is_reported(self):
        s, report = build_suggestions(
            self.assoc([{"batch_id": "b000", "thread_key": "typo"}]),
            self.threads, resolve, run=1)
        self.assertEqual(s, [])
        self.assertEqual(report["missing_threads"][0]["thread_key"], "typo")

    def test_an_unresolvable_memory_is_reported_not_silently_dropped(self):
        (self.dir / "b002.json").write_text(json.dumps(
            profile("b002", [thread("t3", ["gone"])], session="s3")))
        threads = load_threads(self.dir)
        _, report = build_suggestions(self.assoc([{"batch_id": "b002", "thread_key": "t3"}]),
                                      threads, resolve, run=1)
        self.assertEqual(report["unresolved_evidence"], ["gone"])

    def test_source_refs_pass_through_without_a_hash(self):
        (self.dir / "doc.json").write_text(json.dumps(
            profile("doc", [thread("t4", ["sources/a.md#Setup"])], session="source:a.md")))
        threads = load_threads(self.dir)
        s, _ = build_suggestions(self.assoc([{"batch_id": "doc", "thread_key": "t4"}]),
                                 threads, resolve, run=1)
        self.assertEqual(s[0]["evidence"], [{"ref": "sources/a.md#Setup", "sha256": None}])

    def test_a_rejected_topic_is_not_proposed_again(self):
        ledger = self.dir / "decisions.md"
        ledger.write_text("k | rejected | 2026-07-31 | not worth it\n")
        s, report = build_suggestions(self.assoc([{"batch_id": "b000", "thread_key": "t1"}]),
                                      self.threads, resolve, run=1, ledger=ledger)
        self.assertEqual(s, [])
        self.assertEqual(report["suppressed_by_ledger"], ["k"])

    def test_single_session_blogs_are_surfaced_for_review(self):
        s, report = build_suggestions(self.assoc([{"batch_id": "b000", "thread_key": "t1"}]),
                                      self.threads, resolve, run=1)
        self.assertEqual(report["single_session_blogs"], [s[0]["id"]])

    def test_ids_are_sequential_and_run_scoped(self):
        topics = self.assoc([{"batch_id": "b000", "thread_key": "t1"}]) + \
            [{"topic_key": "k2", "type": "docs-new", "title": "T2", "value": "v",
              "members": [{"batch_id": "b001", "thread_key": "t2"}]}]
        s, _ = build_suggestions(topics, self.threads, resolve, run=7)
        self.assertEqual([x["id"] for x in s], ["run-007-S001", "run-007-S002"])

    def test_superseded_profiles_are_not_loaded(self):
        (self.dir / "b000.superseded-abc123.json").write_text(json.dumps(
            profile("b000", [thread("old", ["m1"])])))
        self.assertNotIn(("b000", "old"), load_threads(self.dir))

    def test_suppressed_topics_reads_only_rejections(self):
        ledger = self.dir / "d.md"
        ledger.write_text("a | rejected | d | n\nb | accepted | d | n\nc | deferred | d | n\n")
        self.assertEqual(suppressed_topics(ledger), {"a"})


if __name__ == "__main__":
    unittest.main()
