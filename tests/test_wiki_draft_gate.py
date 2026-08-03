"""Nothing unreviewed leaves the machine.

The sanitizer catches what it can recognize by shape. A personal name it
cannot, so those become flags — and a flag nobody has ruled on must stop the
call rather than ride along in the payload.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memline.wiki import draft as wiki_draft
from memline.wiki.draft import UnreviewedMaterialError, draft_topic, load_review


class _Result:
    """Enough of a CallResult for the gate tests; the network is not the subject."""
    seconds = 1.0
    model = "test"
    usage: dict = {}
    provenance: dict = {}


DRAFT_JSON = {"title": "T", "summary": "What this page is for.",
              "article_markdown": "body", "claims": [],
              "open_questions": [], "unused_evidence_refs": []}

NAMED = "reviewed by 何斌 on the prefill host"


def execute(op, args):
    if op == "get":
        return {"memory": NAMED, "created_at": "2026-07-01T00:00:00", "metadata": {}}
    return {"heads": [args["memory_id"]]}


def topic():
    return {"id": "run-001-S001", "topic_key": "t", "title": "T", "scope": "s",
            "evidence": [{"ref": "mem:11111111-1111-1111-1111-111111111111"}]}


class GateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def draft(self, review=None):
        path = None
        if review is not None:
            path = self.dir / "review.json"
            path.write_text(json.dumps(review), encoding="utf-8")
        with mock.patch.object(wiki_draft, "call_json",
                               return_value=(DRAFT_JSON, _Result())):
            return draft_topic(topic(), execute, "{material}", self.dir,
                               wiki_root=self.dir, review_file=path, log=lambda m: None)

    def test_an_unruled_name_blocks_the_call(self):
        with self.assertRaises(UnreviewedMaterialError) as caught:
            self.draft()
        self.assertIn("何斌", str(caught.exception))

    def test_the_error_names_the_values_so_they_can_be_ruled_on(self):
        with self.assertRaises(UnreviewedMaterialError) as caught:
            self.draft(review={"redact": {}, "cleared": []})
        self.assertIn("ruled on", str(caught.exception))

    def test_a_redacted_name_never_reaches_the_bundle(self):
        self.draft(review={"redact": {"何斌": "PERSON"}, "cleared": []})
        bundle = json.loads((self.dir / "t.bundle.json").read_text(encoding="utf-8"))
        text = bundle["memories"][0]["text"]
        self.assertNotIn("何斌", text)
        self.assertIn("<PERSON-1>", text)

    def test_a_cleared_value_stops_blocking_without_being_replaced(self):
        self.draft(review={"redact": {}, "cleared": ["何斌"]})
        bundle = json.loads((self.dir / "t.bundle.json").read_text(encoding="utf-8"))
        self.assertIn("何斌", bundle["memories"][0]["text"])

    def test_the_retrieval_summary_is_saved_for_review_and_publication(self):
        self.draft(review={"redact": {}, "cleared": ["何斌"]})
        claims = json.loads((self.dir / "t.claims.json").read_text(encoding="utf-8"))
        self.assertEqual(claims["summary"], "What this page is for.")

    def test_rulings_are_read_from_the_file(self):
        path = self.dir / "r.json"
        path.write_text(json.dumps({"redact": {"a": "PERSON"}, "cleared": ["b"]}))
        self.assertEqual(load_review(path), ({"a": "PERSON"}, {"b"}))

    def test_a_missing_review_file_is_not_an_error_by_itself(self):
        self.assertEqual(load_review(self.dir / "nope.json"), ({}, set()))


if __name__ == "__main__":
    unittest.main()
