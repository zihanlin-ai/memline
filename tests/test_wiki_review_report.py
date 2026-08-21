"""Grading the reviewer's reply, with no endpoint in reach.

Every test here hands the grader a report and the bundle it claims to be
about. Nothing calls out, because the point of the split is that how a report
was obtained must not be able to influence how it is judged.

The merge tests encode one rule in both directions: the strictest verdict
wins. A claim any pass called contradicted stays contradicted; an article any
pass wanted revised stays revised. Averaging would let a majority of shallow
passes outvote the one that read the evidence.
"""

from __future__ import annotations

import unittest

from memline.wiki.adjudication import build_adjudication, validate_adjudication
from memline.wiki.artifacts import content_hash
from memline.wiki.review import build_review_bundle
from memline.wiki.review_report import (
    merge_reviews,
    validate_merged_review_report,
    validate_review_artifact,
    validate_review_report,
)
from test_wiki_review import BUNDLE, full_claims, passing_report


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


class SensitivityQuoteTest(unittest.TestCase):
    """A leak claim names a string. Whether it is in the article is not an opinion.

    The first live audit reported four sensitive identifiers and two colleagues'
    names as published leaks. None of the six were in the article — all were in
    the evidence packets quoted alongside it, which is where internal
    identifiers are supposed to live. One finding even carried a line number
    for a sentence that says "a colleague". The fixtures below are invented:
    a test for a leak detector is the last place to paste a real one.
    """

    def setUp(self):
        self.bundle = {
            "article_markdown": "# T\n\nThe patch landed on stack ab93.\n",
            "review_bundle_sha256": "x", "article_sha256": "y",
            "claim_packets": [], "uncited_evidence": [],
        }
        self.bundle["review_bundle_sha256"] = content_hash(
            self.bundle, "review_bundle_sha256"
        )
        self.report = {
            "summary": "s",
            "article_sha256": "y",
            "review_bundle_sha256": self.bundle["review_bundle_sha256"],
            "claim_reviews": [],
            "scope_review": {"verdict": "in_scope", "reason": "r"},
            "overall_verdict": "pass",
            "omission_reviews": [],
        }

    def _kinds(self, omission):
        report = {**self.report, "omission_reviews": [omission]}
        return {f["kind"] for f in validate_review_report(report, self.bundle)["findings"]}

    def test_a_quote_that_is_in_the_article_passes(self):
        self.assertNotIn("article_quote_not_in_article", self._kinds(
            {"severity": "warning", "kind": "sensitivity", "finding": "stack id",
             "article_quotes": ["ab93"]}))

    def test_a_quote_only_present_in_the_evidence_is_rejected(self):
        kinds = self._kinds({"severity": "critical", "kind": "sensitivity",
                             "finding": "names a colleague", "article_quotes": ["Jordan Reyes"]})
        self.assertIn("article_quote_not_in_article", kinds)

    def test_a_sensitivity_finding_must_quote_something(self):
        self.assertIn("sensitivity_finding_without_quotes", self._kinds(
            {"severity": "critical", "kind": "sensitivity", "finding": "leaks ticket ids"}))

    def test_other_kinds_need_no_quotes(self):
        # An omission is about what is *absent*; there is nothing to quote.
        self.assertNotIn("sensitivity_finding_without_quotes", self._kinds(
            {"severity": "info", "kind": "omission", "finding": "dropped counter-evidence"}))


class MergePassesTest(unittest.TestCase):
    """Passes do not contradict each other; they stop looking at different points.

    Five audits of one unchanged article flagged 17, 1, 1, 5 and 19 claims —
    the last two on the same prompt as each other — for a union of 21 that no
    single pass reached. No pass ever asserted that a claim another had flagged
    was in fact fine. So the merge unions rather than votes: with this much
    spread a majority rule would discard most of what was found, and what it
    discarded would be the findings hardest to see.
    """

    @staticmethod
    def _report(verdicts, overall="revise", valid=True, omissions=()):
        return {
            "overall_verdict": overall,
            "claim_reviews": [{"claim_id": cid, "verdict": v, "confidence": "medium",
                               "reason": f"{cid} says {v}"} for cid, v in verdicts.items()],
            "omission_reviews": list(omissions),
            "validation": {"report_valid": valid, "deterministic_clean": True,
                           "scope_clean": True, "claims_expected": len(verdicts),
                           "claim_verdict_counts": {}},
            "review_provenance": {"model": "m"},
        }

    def test_a_claim_flagged_by_one_pass_stays_flagged(self):
        merged = merge_reviews([
            self._report({"c1": "partially_supported", "c2": "supported"}),
            self._report({"c1": "supported", "c2": "supported"}),
        ])
        c1 = next(c for c in merged["claim_reviews"] if c["claim_id"] == "c1")
        self.assertEqual(c1["verdict"], "partially_supported")
        self.assertEqual((c1["flagged_in"], c1["of_passes"]), (1, 2))

    def test_the_strictest_verdict_wins(self):
        merged = merge_reviews([
            self._report({"c1": "partially_supported"}),
            self._report({"c1": "contradicted"}),
        ])
        self.assertEqual(merged["claim_reviews"][0]["verdict"], "contradicted")

    def test_agreement_is_reported_so_a_reviewer_can_weigh_it(self):
        merged = merge_reviews([
            self._report({"c1": "partially_supported", "c2": "partially_supported"}),
            self._report({"c1": "partially_supported", "c2": "supported"}),
        ])
        self.assertEqual(merged["flagged_claims"], 2)
        self.assertEqual(merged["unanimous_claims"], 1)
        self.assertEqual(merged["single_pass_claims"], 1)

    def test_every_reason_is_kept_with_the_pass_that_gave_it(self):
        merged = merge_reviews([
            self._report({"c1": "partially_supported"}),
            self._report({"c1": "unverifiable"}),
        ])
        findings = merged["claim_reviews"][0]["findings"]
        self.assertEqual([f["pass"] for f in findings], [1, 2])

    def test_a_supported_verdict_records_no_reason(self):
        merged = merge_reviews([self._report({"c1": "supported"})])
        self.assertEqual(merged["claim_reviews"][0]["findings"], [])

    def test_the_strictest_overall_verdict_wins(self):
        self.assertEqual(merge_reviews([self._report({}, overall="pass"),
                                        self._report({}, overall="reject")])["overall_verdict"],
                         "reject")

    def test_omissions_from_every_pass_survive_tagged(self):
        merged = merge_reviews([
            self._report({}, omissions=[{"severity": "warning", "finding": "a"}]),
            self._report({}, omissions=[{"severity": "info", "finding": "b"}]),
        ])
        self.assertEqual([(o["finding"], o["pass"]) for o in merged["omission_reviews"]],
                         [("a", 1), ("b", 2)])

    def test_one_invalid_pass_invalidates_the_merge(self):
        # A report that broke the contract cannot vouch for the ones that did not.
        merged = merge_reviews([self._report({}), self._report({}, valid=False)])
        self.assertFalse(merged["validation"]["report_valid"])
        self.assertEqual([p["report_valid"] for p in merged["validation"]["per_pass"]],
                         [True, False])

    def test_the_invalid_pass_is_named_rather_than_left_to_colour_everything(self):
        # One pass quoting a string the article does not contain should not
        # read as "this whole audit is untrustworthy" when two others were
        # clean and every pass's findings are still in the union.
        merged = merge_reviews([self._report({"c1": "partially_supported"}),
                                self._report({}, valid=False),
                                self._report({})])
        self.assertEqual(merged["validation"]["invalid_passes"], [2])
        self.assertEqual(merged["flagged_claims"], 1)

    def test_a_later_run_adds_passes_instead_of_replacing_them(self):
        # Re-auditing an unchanged article returned 5 findings once and 19
        # another time. Replacing would have thrown away fourteen real ones
        # while looking like an update.
        first = merge_reviews([self._report({"c1": "partially_supported", "c2": "supported"})])
        second = merge_reviews([self._report({"c1": "supported", "c2": "unverifiable"})],
                               prior=first)
        self.assertEqual(second["passes"], 2)
        by_id = {c["claim_id"]: c for c in second["claim_reviews"]}
        self.assertEqual(by_id["c1"]["verdict"], "partially_supported")
        self.assertEqual(by_id["c2"]["verdict"], "unverifiable")
        self.assertEqual((by_id["c1"]["flagged_in"], by_id["c1"]["of_passes"]), (1, 2))
        self.assertEqual([f["pass"] for f in by_id["c2"]["findings"]], [2])

    def test_accumulated_provenance_records_every_pass_ever_run(self):
        first = merge_reviews([self._report({})])
        second = merge_reviews([self._report({}), self._report({})], prior=first)
        self.assertEqual(len(second["review_provenance"]), 3)
        self.assertEqual([p["pass"] for p in second["validation"]["per_pass"]], [1, 2, 3])

    def test_merging_nothing_is_an_error_not_an_empty_pass(self):
        with self.assertRaises(ValueError):
            merge_reviews([])


class MergedReviewValidationTest(unittest.TestCase):
    def setUp(self):
        article = ("# T\n\nAlpha."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.bundle = build_review_bundle(article, BUNDLE, full_claims(), {"scope": "alpha"})

    def _pass(self):
        report = passing_report(self.bundle)
        report["review_provenance"] = {"model": "reviewer"}
        report["validation"] = validate_review_report(report, self.bundle)
        return report

    def test_clean_merged_report_passes_the_machine_gate(self):
        merged = merge_reviews([self._pass(), self._pass(), self._pass()])
        validation = validate_merged_review_report(merged, self.bundle)
        self.assertEqual(merged["review_bundle_sha256"],
                         self.bundle["review_bundle_sha256"])
        self.assertTrue(validation["report_valid"])
        self.assertTrue(validation["clean"])
        self.assertEqual(validation["claim_verdict_counts"], {"supported": 1})

    def test_artifact_dispatch_uses_the_merged_schema(self):
        merged = merge_reviews([self._pass(), self._pass(), self._pass()])
        validation = validate_review_artifact(merged, self.bundle)
        self.assertTrue(validation["clean"])
        self.assertNotIn("review_summary_missing",
                         {finding["kind"] for finding in validation["findings"]})

    def test_one_contract_invalid_pass_blocks_the_merged_gate(self):
        invalid = self._pass()
        # A warning-level leak claim with nothing to back it. An `info` note
        # saying no sensitive strings appear used to serve here, but reporting
        # an absence proves nothing and no longer breaks the contract.
        invalid["omission_reviews"] = [{
            "severity": "warning", "kind": "sensitivity",
            "finding": "leaks an internal ticket id", "evidence_refs": [],
        }]
        invalid["validation"] = validate_review_report(invalid, self.bundle)
        merged = merge_reviews([self._pass(), invalid, self._pass()])
        validation = validate_merged_review_report(merged, self.bundle)
        kinds = {finding["kind"] for finding in validation["findings"]}
        self.assertIn("merged_review_contains_invalid_passes", kinds)
        self.assertFalse(validation["report_valid"])
        self.assertFalse(validation["clean"])

    def test_merged_report_is_bound_to_the_review_bundle_hash(self):
        merged = merge_reviews([self._pass()])
        merged["review_bundle_sha256"] = "stale"
        validation = validate_merged_review_report(merged, self.bundle)
        self.assertIn("review_hash_mismatch",
                      {finding["kind"] for finding in validation["findings"]})

    def test_scope_reason_is_retained_for_agent_adjudication(self):
        report = self._pass()
        report["scope_review"] = {"verdict": "minor_drift", "reason": "Adjacent topic."}
        report["validation"] = validate_review_report(report, self.bundle)
        merged = merge_reviews([report])
        self.assertEqual(merged["scope_reviews"], [{
            "pass": 1, "verdict": "minor_drift", "reason": "Adjacent topic."
        }])


class AgentAdjudicationTest(unittest.TestCase):
    """The reviewer discovers issues; only the agent materiality ruling gates them."""

    def setUp(self):
        article = ("# T\n\nAlpha."
                   "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.bundle = build_review_bundle(article, BUNDLE, full_claims(), {"scope": "alpha"})

    def _review(self, *, finding=True, overall="revise"):
        report = passing_report(self.bundle)
        report["overall_verdict"] = overall
        if finding:
            report["claim_reviews"][0].update({
                "verdict": "partially_supported", "confidence": "medium",
                "reason": "The wording is slightly broader than the measurement.",
                "suggested_rewrite": "Narrow the wording.",
            })
        report["review_provenance"] = {"model": "reviewer"}
        report["validation"] = validate_review_report(report, self.bundle)
        return merge_reviews([report])

    @staticmethod
    def _rule(template, decision, category):
        template["findings"][0].update({
            "decision": decision, "category": category,
            "reason": "This does not change the result or an engineering decision."
        })
        return template

    def test_a_finding_without_agent_adjudication_blocks(self):
        validation = validate_review_artifact(self._review(), self.bundle)
        self.assertFalse(validation["clean"])
        self.assertEqual(validation["adjudication"]["findings"][0]["kind"],
                         "adjudication_missing")

    def test_non_blocking_adjudication_clears_reviewer_revise(self):
        report = self._review()
        adjudication = self._rule(build_adjudication(report), "non_blocking", "non_material")
        validation = validate_review_artifact(report, self.bundle, adjudication)
        self.assertFalse(validation["reviewer_clean"])
        self.assertTrue(validation["clean"])
        self.assertEqual(validation["adjudication"]["non_blocking_findings"], 1)

    def test_blocking_adjudication_requires_fix_and_new_review(self):
        report = self._review()
        adjudication = self._rule(
            build_adjudication(report), "blocking", "factual_contradiction")
        validation = validate_review_artifact(report, self.bundle, adjudication)
        self.assertFalse(validation["clean"])
        self.assertEqual(validation["adjudication"]["unresolved_blocking_findings"], 1)

    def test_reviewer_overall_verdict_is_not_itself_a_finding(self):
        report = self._review(finding=False, overall="revise")
        validation = validate_review_artifact(report, self.bundle)
        self.assertEqual(validation["reviewer_findings"], 0)
        self.assertTrue(validation["clean"])

    def test_non_blocking_cannot_use_a_blocking_category(self):
        report = self._review()
        adjudication = self._rule(
            build_adjudication(report), "non_blocking", "factual_contradiction")
        validation = validate_adjudication(adjudication, report)
        self.assertFalse(validation["valid"])
        self.assertIn("adjudication_category_invalid",
                      {item["kind"] for item in validation["findings"]})

    def test_malformed_finding_id_is_reported_instead_of_crashing(self):
        report = self._review()
        adjudication = build_adjudication(report)
        adjudication["findings"][0]["finding_id"] = ["not", "hashable"]
        validation = validate_adjudication(adjudication, report)
        self.assertFalse(validation["valid"])
        self.assertIn("adjudication_finding_id_invalid",
                      {item["kind"] for item in validation["findings"]})

    def test_adjudication_is_bound_to_the_exact_review_report(self):
        report = self._review()
        adjudication = self._rule(build_adjudication(report), "non_blocking", "non_material")
        report["overall_verdict"] = "reject"
        validation = validate_adjudication(adjudication, report)
        self.assertIn("adjudication_hash_mismatch",
                      {item["kind"] for item in validation["findings"]})

    def test_contract_invalid_review_cannot_be_adjudicated_away(self):
        report = self._review()
        report["validation"]["per_pass"][0]["report_valid"] = False
        report["validation"]["report_valid"] = False
        report["validation"]["invalid_passes"] = [1]
        adjudication = self._rule(build_adjudication(report), "non_blocking", "non_material")
        validation = validate_review_artifact(report, self.bundle, adjudication)
        self.assertEqual(validation["valid_review_passes"], 0)
        self.assertFalse(validation["clean"])


class SensitivityAbsenceTest(unittest.TestCase):
    """Reporting that nothing leaked is not a claim that needs proving.

    The gate fired on a pass that correctly said "no sensitive strings appear
    in article_markdown" and marked the whole report invalid for it. A gate
    that punishes the right answer is one its reader learns to skip.
    """

    def setUp(self):
        self.bundle = {"article_markdown": "# T\n\nStack ab93.\n",
                       "review_bundle_sha256": "x", "article_sha256": "y",
                       "claim_packets": [], "uncited_evidence": []}
        self.bundle["review_bundle_sha256"] = content_hash(
            self.bundle, "review_bundle_sha256"
        )
        self.report = {"summary": "s", "article_sha256": "y",
                       "review_bundle_sha256": self.bundle["review_bundle_sha256"],
                       "claim_reviews": [], "omission_reviews": [],
                       "scope_review": {"verdict": "in_scope", "reason": "r"},
                       "overall_verdict": "pass"}

    def _kinds(self, omission):
        report = {**self.report, "omission_reviews": [omission]}
        return {f["kind"] for f in validate_review_report(report, self.bundle)["findings"]}

    def test_an_info_clearance_note_needs_no_quotes(self):
        self.assertNotIn("sensitivity_finding_without_quotes", self._kinds(
            {"severity": "info", "kind": "sensitivity",
             "finding": "No sensitive strings appear in article_markdown."}))

    def test_a_warning_still_has_to_prove_itself(self):
        self.assertIn("sensitivity_finding_without_quotes", self._kinds(
            {"severity": "warning", "kind": "sensitivity", "finding": "leaks a ticket id"}))

    def test_a_quote_that_is_close_is_still_wrong(self):
        # "it is the..." for an article that says "the...": a paraphrase is
        # how a finding about evidence gets dressed as one about the article.
        self.assertIn("article_quote_not_in_article", self._kinds(
            {"severity": "warning", "kind": "causal_strength", "finding": "overstated",
             "article_quotes": ["it is Stack ab93"]}))


# --- deployment-config stub -------------------------------------------------
# The sanitizer's internal-domain lists are deployment configuration; these
# tests declare their own so they run identically with or without a workspace
# config.toml on the search path.
from unittest import mock as _mock  # noqa: E402

from memline import config as _config  # noqa: E402

_sanitize_patchers = [
    _mock.patch.object(_config, "SANITIZE_INTERNAL_DOMAINS", ["corp.example.com"]),
    _mock.patch.object(_config, "SANITIZE_INTERNAL_REPO_HOSTS", ["git.example.internal"]),
]


def setUpModule():
    for _p in _sanitize_patchers:
        _p.start()


def tearDownModule():
    for _p in _sanitize_patchers:
        _p.stop()


if __name__ == "__main__":
    unittest.main()
