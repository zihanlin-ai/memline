"""Resume must notice a session that changed under a batch it already profiled."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memline.wiki_profile import coverage_digest, _needs_profiling


def batch(ids, batch_id="b000"):
    return {"batch_id": batch_id, "kind": "session", "memory_ids": list(ids),
            "memory_count": len(ids), "span": ["2026-07-01", "2026-07-02"],
            "session_ids": ["s1"]}


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, b, covers=None):
        (self.dir / f"{b['batch_id']}.json").write_text(json.dumps(
            {"batch_id": b["batch_id"], "status": "ok", "profile": {},
             "covers": covers if covers is not None else coverage_digest(b)}))

    def test_missing_artifact_is_profiled(self):
        self.assertTrue(_needs_profiling(batch(["a"]), self.dir, lambda m: None))

    def test_unchanged_batch_is_skipped(self):
        b = batch(["a", "b"])
        self.write(b)
        self.assertFalse(_needs_profiling(b, self.dir, lambda m: None))

    def test_order_of_ids_does_not_count_as_a_change(self):
        self.write(batch(["a", "b"]))
        self.assertFalse(_needs_profiling(batch(["b", "a"]), self.dir, lambda m: None))

    def test_a_grown_session_is_reprofiled(self):
        self.write(batch(["a", "b"]))
        self.assertTrue(_needs_profiling(batch(["a", "b", "c"]), self.dir, lambda m: None))

    def test_the_superseded_profile_is_kept(self):
        old = batch(["a", "b"])
        self.write(old)
        _needs_profiling(batch(["a", "b", "c"]), self.dir, lambda m: None)
        kept = list(self.dir.glob("b000.superseded-*.json"))
        self.assertEqual(len(kept), 1)
        self.assertFalse((self.dir / "b000.json").exists())

    def test_an_artifact_without_coverage_is_reprofiled(self):
        # Profiles written before coverage was recorded cannot be trusted to
        # describe the same memories, so they are re-read once.
        self.write(batch(["a"]), covers=None)
        (self.dir / "b000.json").write_text(json.dumps({"batch_id": "b000", "status": "ok"}))
        self.assertTrue(_needs_profiling(batch(["a"]), self.dir, lambda m: None))

    def test_an_unreadable_artifact_is_reprofiled(self):
        (self.dir / "b000.json").write_text("{ not json")
        self.assertTrue(_needs_profiling(batch(["a"]), self.dir, lambda m: None))


if __name__ == "__main__":
    unittest.main()
