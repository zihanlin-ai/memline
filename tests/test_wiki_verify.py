"""Verifying a draft against its bundle.

Each test pins one failure that would otherwise reach a human reader looking
like diligence: an invented citation, an unsupported section, a redaction the
writer filled in, or a number that exists nowhere in the material.
"""

from __future__ import annotations

import unittest

from memline.wiki.verify import verify

BUNDLE = {
    "memories": [
        {"id": "aaaa1111-2222-3333-4444-555566667777", "text": "cosine fell to 0.11 at K=65536",
         "superseded": False},
        {"id": "bbbb1111-2222-3333-4444-555566667777", "text": "we believed the guard applied",
         "superseded": True},
    ],
    "source_sections": [{"ref": "sources/doc.md#Matrix", "text": "OmniCache excludes CP"}],
}


def draft(body):
    return "# Title\n\n" + body


class VerifyTest(unittest.TestCase):
    def kinds(self, body, claims=None):
        return [f["kind"] for f in verify(draft(body), BUNDLE, claims)["findings"]]

    def test_a_clean_draft_has_no_findings(self):
        body = ("cosine fell to 0.11 at K=65536.^[mem:aaaa1111-2222-3333-4444-555566667777] "
                "OmniCache excludes CP.^[sources/doc.md#Matrix]")
        self.assertEqual(self.kinds(body), [])

    def test_a_claims_sidecar_without_a_page_summary_is_not_publishable(self):
        body = ("cosine fell to 0.11 at K=65536."
                "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.assertIn("page_summary_missing", self.kinds(body, {"claims": []}))

    def test_a_claims_sidecar_with_a_page_summary_passes_the_summary_gate(self):
        body = ("cosine fell to 0.11 at K=65536."
                "^[mem:aaaa1111-2222-3333-4444-555566667777]")
        self.assertNotIn("page_summary_missing",
                         self.kinds(body, {"summary": "What the measurement establishes.",
                                           "claims": []}))

    def test_an_invented_citation_is_caught(self):
        body = "a claim.^[mem:dddd0000-0000-0000-0000-000000000000]"
        self.assertIn("citation_not_in_bundle", self.kinds(body))

    def test_a_malformed_citation_is_caught(self):
        body = "a claim.^[mem:short]"
        self.assertIn("citation_invalid_format", self.kinds(body))

    def test_a_citation_missing_its_caret_is_caught(self):
        body = "a claim.[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("citation_missing_caret", self.kinds(body))

    def test_a_section_with_no_citation_is_caught(self):
        body = ("intro.^[mem:aaaa1111-2222-3333-4444-555566667777]\n\n"
                "## Findings\n\nWe concluded the obvious.\n")
        self.assertIn("section_without_citation", self.kinds(body))

    def test_a_hierarchy_only_heading_does_not_need_a_citation(self):
        body = ("intro.^[mem:aaaa1111-2222-3333-4444-555566667777]\n\n"
                "## Group\n\n### Finding\n\n"
                "supported.^[mem:aaaa1111-2222-3333-4444-555566667777]\n")
        self.assertNotIn("section_without_citation", self.kinds(body))

    def test_a_filled_in_placeholder_is_caught(self):
        body = "the host 192.0.2.39 failed.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("redaction_lost_ipv4", self.kinds(body))

    def test_an_account_id_is_caught(self):
        body = "run by u01234567.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("redaction_lost_account_id", self.kinds(body))

    def test_a_number_absent_from_the_material_is_caught(self):
        body = "throughput reached 25900 tok/s.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("number_not_in_material", self.kinds(body))

    def test_a_number_present_in_the_material_is_not_flagged(self):
        body = "the cliff is at 65536.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertNotIn("number_not_in_material", self.kinds(body))

    def test_a_claim_citing_outside_the_bundle_is_caught(self):
        claims = {"claims": [{"claim": "x", "evidence_refs": ["mem:cccc0000-0000-0000-0000-000000000000"]}]}
        body = "text.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("claim_ref_not_in_bundle", self.kinds(body, claims))

    def test_a_non_ref_hidden_in_retraction_evidence_is_caught(self):
        claims = {"claims": [], "retraction_arcs": [{"evidence_refs": ["mems: prose"]}],
                  "unused_evidence_refs": [
                      "mem:bbbb1111-2222-3333-4444-555566667777",
                      "sources/doc.md#Matrix",
                  ]}
        body = "text.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("sidecar_ref_invalid_format", self.kinds(body, claims))

    def test_evidence_cannot_be_both_cited_and_declared_unused(self):
        claims = {"claims": [],
                  "unused_evidence_refs": ["mem:aaaa1111-2222-3333-4444-555566667777",
                                           "mem:bbbb1111-2222-3333-4444-555566667777",
                                           "sources/doc.md#Matrix"]}
        body = "text.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("evidence_both_cited_and_unused", self.kinds(body, claims))

    def test_every_bundle_ref_must_be_cited_or_declared_unused(self):
        claims = {"claims": [], "unused_evidence_refs": []}
        body = "text.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("evidence_neither_cited_nor_unused", self.kinds(body, claims))

    def test_an_invented_unused_ref_is_caught(self):
        claims = {"claims": [],
                  "unused_evidence_refs": ["mem:cccc0000-0000-0000-0000-000000000000"]}
        body = "text.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("unused_ref_not_in_bundle", self.kinds(body, claims))

    def test_citations_of_superseded_evidence_are_reported_not_failed(self):
        body = ("this was believed.^[mem:bbbb1111-2222-3333-4444-555566667777]")
        report = verify(draft(body), BUNDLE)
        self.assertEqual(report["cited_superseded"], ["mem:bbbb1111-2222-3333-4444-555566667777"])
        self.assertNotIn("superseded", " ".join(f["kind"] for f in report["findings"]))

    def test_uncited_material_is_counted(self):
        body = "only one.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertEqual(verify(draft(body), BUNDLE)["uncited_material"], 2)


if __name__ == "__main__":
    unittest.main()


class AbbreviationTest(unittest.TestCase):
    """A shortened id is a formatting slip; an invented one is not."""

    def report(self, body):
        return verify(draft(body), BUNDLE)

    def test_an_unambiguous_short_id_is_repaired_not_condemned(self):
        r = self.report("claim.^[mem:aaaa1111]")
        self.assertEqual(r["abbreviated_citations"],
                         {"mem:aaaa1111": "mem:aaaa1111-2222-3333-4444-555566667777"})
        self.assertEqual([f["kind"] for f in r["findings"]], ["citation_abbreviated"])

    def test_a_repaired_citation_counts_towards_coverage(self):
        self.assertGreater(self.report("claim.^[mem:aaaa1111]")["coverage"], 0)

    def test_a_repaired_citation_of_superseded_evidence_still_surfaces(self):
        self.assertEqual(self.report("was believed.^[mem:bbbb1111]")["cited_superseded"],
                         ["mem:bbbb1111-2222-3333-4444-555566667777"])

    def test_an_id_that_matches_nothing_is_still_a_fabrication(self):
        kinds = [f["kind"] for f in self.report("claim.^[mem:99999999]")["findings"]]
        self.assertIn("citation_not_in_bundle", kinds)

    def test_a_full_length_id_off_by_one_character_is_not_repaired(self):
        # The dangerous case: it looks entirely legitimate and is not.
        kinds = [f["kind"] for f in
                 self.report("claim.^[mem:aaaa1111-2222-3333-4444-555566667778]")["findings"]]
        self.assertEqual(kinds, ["citation_not_in_bundle"])


class NumberNoiseTest(unittest.TestCase):
    def test_digits_inside_a_citation_are_not_read_as_numbers(self):
        body = "a claim.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        kinds = [f["kind"] for f in verify(draft(body), BUNDLE)["findings"]]
        self.assertNotIn("number_not_in_material", kinds)

    def test_a_real_number_beside_a_citation_is_still_checked(self):
        body = "throughput hit 25900.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        kinds = [f["kind"] for f in verify(draft(body), BUNDLE)["findings"]]
        self.assertIn("number_not_in_material", kinds)


class LeakPatternTest(unittest.TestCase):
    """Identifiers the bundle cannot redact by shape, checked in the article.

    The sanitizer replaces what it recognises; an issue id, a merge request, an
    internal path and a build tag it cannot. Those were left to a sentence in
    the prompt asking the writer to omit them, and one article carried all four
    into its prose — each named in that topic's own sensitivity notes. An
    instruction the program cannot check is one that holds until it doesn't.
    """

    def _kinds(self, text):
        bundle = {"memories": [], "source_sections": []}
        return {f["kind"][len("redaction_lost_"):]: f["detail"]
                for f in verify(text, bundle)["findings"]
                if f["kind"].startswith("redaction_lost_")}

    def test_internal_tracker_ids_are_found(self):
        self.assertEqual(self._kinds("Filed as IZZ9QW.").get("issue_id"), "IZZ9QW")

    def test_all_caps_words_are_not_mistaken_for_tracker_ids(self):
        # Without the leading-I anchor this claimed README, REJECT, SHA256 and
        # PEP604 — four false positives against two real ids in one draft.
        for word in ("README", "REJECT", "SHA256", "PEP604"):
            with self.subTest(word=word):
                self.assertNotIn("issue_id", self._kinds(f"See {word} for details."))

    def test_merge_and_pull_request_numbers_are_found(self):
        self.assertIn("change_id", self._kinds("Fixed by !9001."))
        self.assertIn("change_id", self._kinds("Upstreamed as #36030."))

    def test_internal_absolute_paths_are_found(self):
        self.assertEqual(self._kinds("Run /workspace/scripts/example_suite next.")
                         .get("internal_path"), "/workspace/scripts/example_suite")

    def test_internal_build_tags_are_found(self):
        self.assertEqual(self._kinds("Image vllm-209901010000 failed to pull.")
                         .get("image_tag"), "vllm-209901010000")

    def test_measurements_are_never_read_as_a_host_pool(self):
        # A rule for two-octet host pools was tried and removed: a pool written
        # that way and a ratio are the same shape, so it fired 28-45 times per
        # draft and was wrong every time. A gate that is always wrong teaches
        # its reader to skip the report, which costs more than the leak it was
        # meant to catch.
        text = "Utilisation went 0.93 to 1.05 and throughput 14.55 to 22.66."
        self.assertEqual(self._kinds(text), {})

    def test_public_identifiers_still_pass(self):
        # Public model names, versions and commit hashes are allowed to stay;
        # a rule that removed them would make the articles unusable.
        text = "DeepSeek-V4-285B at commit a5c61021 on vLLM 0.9.1 gave 22,000 tok/s."
        self.assertEqual(self._kinds(text), {})
