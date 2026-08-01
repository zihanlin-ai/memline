"""Deterministic review packets and validation of an external review."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mem0_local.wiki_review import (
    build_review_bundle,
    run_external_review,
    validate_review_report,
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


class ReviewValidationTest(unittest.TestCase):
    def setUp(self):
        article = ("# T\n\nAlpha."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.bundle = build_review_bundle(article, BUNDLE, full_claims(), {"scope": "alpha"})

    def test_complete_supported_report_passes_the_machine_gate(self):
        validation = validate_review_report(passing_report(self.bundle), self.bundle)
        self.assertTrue(validation["report_valid"])
        self.assertTrue(validation["clean"])
        self.assertTrue(validation["agent_review_required"])
        self.assertEqual(validation["claims_reviewed"], 1)
        self.assertEqual(validation["claim_verdict_counts"], {"supported": 1})

    def test_stale_hash_and_missing_claim_review_fail_validation(self):
        report = passing_report(self.bundle)
        report["article_sha256"] = "stale"
        report["claim_reviews"] = []
        validation = validate_review_report(report, self.bundle)
        kinds = {finding["kind"] for finding in validation["findings"]}
        self.assertIn("review_hash_mismatch", kinds)
        self.assertIn("claim_reviews_incomplete", kinds)
        self.assertFalse(validation["clean"])

    def test_claim_review_cannot_import_evidence_from_elsewhere(self):
        report = passing_report(self.bundle)
        report["claim_reviews"][0]["evidence_refs"] = ["sources/doc.md#Rule"]
        validation = validate_review_report(report, self.bundle)
        self.assertIn("claim_review_evidence_outside_packet",
                      {finding["kind"] for finding in validation["findings"]})

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


if __name__ == "__main__":
    unittest.main()
