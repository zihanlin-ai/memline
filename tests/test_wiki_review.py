"""The evidence packet, and the call that asks a reviewer to judge it.

What the packet contains is the whole leverage of this design: a reviewer that
is handed the exact material a citation resolves to cannot invent support for
a claim, and a citation that resolves to nothing stays visibly unresolved
rather than being repaired by similarity. Grading the reply is a separate
concern and lives in ``test_wiki_review_report``.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from pathlib import Path

from memline.wiki.review import (
    _content_hash,
    build_review_bundle,
    load_prior_review,
    run_external_review,
)

BUNDLE = {
    "sanitized": True,
    "sanitization": {"review_flags": []},
    "unresolved": [],
    "memories": [
        {"id": "aaaa1111-2222-3333-4444-555566667777", "text": "alpha reached 0.11",
         "sha256": "hash-a", "superseded": False},
        {"id": "bbbb1111-2222-3333-4444-555566667777", "text": "the old belief",
         "sha256": "hash-b", "superseded": True},
    ],
    "source_sections": [
        {"ref": "sources/doc.md#Rule", "text": "the settled rule", "sha256": "hash-s",
         "document": "sources/doc.md", "heading": "Rule"},
    ],
}


def full_claims():
    return {
        "summary": "What the alpha measurement establishes.",
        "claims": [],
        "unused_evidence_refs": [
            "mem:bbbb1111-2222-3333-4444-555566667777", "sources/doc.md#Rule"
        ],
    }


def passing_report(review_bundle):
    return {
        "article_sha256": review_bundle["article_sha256"],
        "review_bundle_sha256": review_bundle["review_bundle_sha256"],
        "overall_verdict": "pass",
        "summary": "supported",
        "claim_reviews": [
            {"claim_id": packet["claim_id"], "verdict": "supported", "confidence": "high",
             "reason": "exactly stated", "evidence_refs": [
                 citation["resolved_ref"] for citation in packet["citations"]
                 if citation["resolved_ref"]
             ], "suggested_rewrite": ""}
            for packet in review_bundle["claim_packets"]
        ],
        "omission_reviews": [],
        "scope_review": {"verdict": "in_scope", "reason": "matches"},
    }


class ReviewBundleTest(unittest.TestCase):
    def test_exact_evidence_is_attached_to_each_citation_occurrence(self):
        article = ("# T\n\nAlpha reached 0.11."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        review = build_review_bundle(article, BUNDLE, full_claims(), {"scope": "alpha"})
        citation = review["claim_packets"][0]["citations"][0]
        self.assertEqual(citation["status"], "exact")
        self.assertEqual(citation["evidence"]["text"], "alpha reached 0.11")
        self.assertEqual(review["claim_packets"][0]["text"], "Alpha reached 0.11.")

    def test_unique_abbreviation_is_resolved_but_remains_visible(self):
        article = "# T\n\nAlpha.^[mem:aaaa1111]"
        review = build_review_bundle(article, BUNDLE)
        citation = review["claim_packets"][0]["citations"][0]
        self.assertEqual(citation["status"], "abbreviated")
        self.assertEqual(citation["resolved_ref"],
                         "mem:aaaa1111-2222-3333-4444-555566667777")

    def test_missing_reference_is_marked_and_never_given_evidence(self):
        article = "# T\n\nAlpha.^[mem:cccc0000]"
        review = build_review_bundle(article, BUNDLE)
        citation = review["claim_packets"][0]["citations"][0]
        self.assertEqual(citation["status"], "missing")
        self.assertIsNone(citation["resolved_ref"])
        self.assertIsNone(citation["evidence"])

    def test_uncited_material_and_passages_are_exposed_for_omission_review(self):
        article = ("# T\n\nAlpha."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]\n\nAn uncited conclusion.")
        review = build_review_bundle(article, BUNDLE)
        self.assertEqual(review["uncited_passages"][0]["text"], "An uncited conclusion.")
        self.assertEqual({e["ref"] for e in review["uncited_evidence"]}, {
            "mem:bbbb1111-2222-3333-4444-555566667777", "sources/doc.md#Rule"
        })


class ExternalReviewCallTest(unittest.TestCase):
    """The endpoint call itself: what is sent, and what is recorded about it."""

    def setUp(self):
        article = ("# T\n\nAlpha."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.bundle = build_review_bundle(article, BUNDLE, full_claims(), {"scope": "alpha"})

    def test_external_result_is_saved_with_provenance_and_local_validation(self):
        @dataclass
        class Result:
            provenance: dict

        expected = passing_report(self.bundle)

        def caller(prompt, **kwargs):
            self.assertIn(self.bundle["review_bundle_sha256"], prompt)
            self.assertEqual(kwargs["job"], "review")
            return dict(expected), Result({"model": "k3", "endpoint": "test"})

        report = run_external_review(self.bundle, "Review:\n{review_bundle}", caller=caller)
        self.assertEqual(report["review_provenance"]["model"], "k3")
        self.assertTrue(report["validation"]["clean"])

class PriorReviewTest(unittest.TestCase):
    """Accumulation is safe only because the article's hash gates it."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "x.review.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, **fields):
        self.path.write_text(json.dumps({"passes": 3, **fields}), encoding="utf-8")

    def test_a_review_of_the_same_article_is_returned(self):
        self._write(article_sha256="aaa")
        self.assertEqual(load_prior_review(self.path, "aaa")["passes"], 3)

    def test_a_review_of_a_different_article_is_not(self):
        # Its findings describe sentences that may no longer exist.
        self._write(article_sha256="aaa")
        self.assertIsNone(load_prior_review(self.path, "bbb"))

    def test_a_review_of_a_different_evidence_packet_is_not_reused(self):
        self._write(article_sha256="aaa", review_bundle_sha256="bundle-a")
        self.assertIsNone(load_prior_review(self.path, "aaa", "bundle-b"))

    def test_a_review_with_no_hash_is_not_reused(self):
        # Reports written before the hash was recorded cannot be matched, and
        # guessing they belong to this article is exactly the wrong default.
        self._write()
        self.assertIsNone(load_prior_review(self.path, "aaa"))

    def test_a_missing_or_unreadable_file_is_simply_no_prior(self):
        self.assertIsNone(load_prior_review(self.path, "aaa"))
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_prior_review(self.path, "aaa"))

    def test_a_single_pass_report_from_before_the_merge_is_ignored(self):
        self.path.write_text(json.dumps({"article_sha256": "aaa", "claim_reviews": []}),
                             encoding="utf-8")
        self.assertIsNone(load_prior_review(self.path, "aaa"))


if __name__ == "__main__":
    unittest.main()
