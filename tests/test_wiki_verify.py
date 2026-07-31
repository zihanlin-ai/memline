"""Verifying a draft against its bundle.

Each test pins one failure that would otherwise reach a human reader looking
like diligence: an invented citation, an unsupported section, a redaction the
writer filled in, or a number that exists nowhere in the material.
"""

from __future__ import annotations

import unittest

from mem0_local.wiki_verify import verify

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

    def test_an_invented_citation_is_caught(self):
        body = "a claim.^[mem:dddd0000-0000-0000-0000-000000000000]"
        self.assertIn("citation_not_in_bundle", self.kinds(body))

    def test_a_section_with_no_citation_is_caught(self):
        body = ("intro.^[mem:aaaa1111-2222-3333-4444-555566667777]\n\n"
                "## Findings\n\nWe concluded the obvious.\n")
        self.assertIn("section_without_citation", self.kinds(body))

    def test_a_filled_in_placeholder_is_caught(self):
        body = "the host 7.150.10.239 failed.^[mem:aaaa1111-2222-3333-4444-555566667777]"
        self.assertIn("redaction_lost_ipv4", self.kinds(body))

    def test_an_account_id_is_caught(self):
        body = "run by l00959355.^[mem:aaaa1111-2222-3333-4444-555566667777]"
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
