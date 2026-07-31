"""Unit tests for outbound bundling and its sanitizer.

The properties that matter are: distinct sensitive values stay distinct after
substitution, the mapping never rides along inside the bundle, recorded
hashes describe the ORIGINAL text, and unrecognizable-but-suspicious shapes
are reported rather than dropped.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mem0_local.bundle import Sanitizer, build_bundle, review_flags, sha256_text

MEM_A = "11111111-1111-1111-1111-111111111111"
MEM_B = "22222222-2222-2222-2222-222222222222"
TEXT_A = "KV link from 7.150.10.239 to 7.150.12.255 failed; logs under /data/l00959355/run1"
TEXT_B = "Retried on 7.150.10.239 with image from https://gitee.com/org/repo after 127.0.0.1 check"


def make_execute(texts, head=None):
    def execute(op, args):
        mid = args["memory_id"]
        if op == "get":
            if mid not in texts:
                raise RuntimeError("not found")
            return {"memory": texts[mid], "created_at": "2026-07-13T00:00:00",
                    "metadata": {"writer_agent_id": "claude"}}
        if op == "resolve_head":
            return head or {"heads": [mid]}
        raise AssertionError(op)
    return execute


class SanitizerTest(unittest.TestCase):
    def test_distinct_hosts_get_distinct_placeholders(self):
        s = Sanitizer()
        out = s.scrub(TEXT_A)
        self.assertIn("<HOST-1>", out)
        self.assertIn("<HOST-2>", out)
        self.assertNotIn("7.150.10.239", out)

    def test_same_value_is_stable_across_texts(self):
        s = Sanitizer()
        first = s.scrub(TEXT_A)
        second = s.scrub(TEXT_B)
        host = first.split(" ")[3]
        self.assertIn(host, second)

    def test_account_id_inside_path_is_replaced_but_path_shape_survives(self):
        out = Sanitizer().scrub(TEXT_A)
        self.assertIn("/data/<USER-1>/run1", out)

    def test_loopback_is_kept(self):
        self.assertIn("127.0.0.1", Sanitizer().scrub(TEXT_B))

    def test_internal_repo_url_is_replaced(self):
        self.assertNotIn("gitee.com", Sanitizer().scrub(TEXT_B))

    def test_mapping_restores_originals(self):
        s = Sanitizer()
        s.scrub(TEXT_A)
        self.assertIn("7.150.10.239", s.mapping.values())


class ReviewFlagTest(unittest.TestCase):
    def test_email_is_flagged(self):
        flags = review_flags({MEM_A: "ask alice@example.com about it"})
        self.assertEqual([f["kind"] for f in flags], ["email"])

    def test_clean_text_has_no_flags(self):
        self.assertEqual(review_flags({MEM_A: "vLLM v0.14.0 on Ascend A3"}), [])


class SourceSectionTest(unittest.TestCase):
    """A citation points at a section, and the bundle must carry that section."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sources").mkdir()
        (self.root / "sources" / "doc.md").write_text(
            "# Title\n\nintro\n\n## Quick matrix\n\nrow one\nrow two\n\n"
            "### Detail\n\nnested\n\n## Other\n\nelsewhere\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def bundle(self, ref):
        return build_bundle([ref], make_execute({}), wiki_root=self.root)[0]

    def test_section_stops_at_the_next_heading_of_the_same_level(self):
        text = self.bundle("sources/doc.md#Quick matrix")["source_sections"][0]["text"]
        self.assertIn("row two", text)
        self.assertNotIn("elsewhere", text)

    def test_section_keeps_its_nested_subsections(self):
        self.assertIn("nested", self.bundle("sources/doc.md#Quick matrix")["source_sections"][0]["text"])

    def test_heading_match_ignores_level_and_case(self):
        self.assertEqual(len(self.bundle("sources/doc.md#quick MATRIX")["source_sections"]), 1)

    def test_whole_file_when_no_heading_is_given(self):
        self.assertIn("elsewhere", self.bundle("sources/doc.md")["source_sections"][0]["text"])

    def test_missing_section_is_reported_not_silently_empty(self):
        b = self.bundle("sources/doc.md#Nope")
        self.assertEqual(b["source_sections"], [])
        self.assertEqual(b["unresolved"][0]["error"], "section not found")

    def test_path_escaping_the_wiki_root_is_refused(self):
        b = self.bundle("sources/../../etc/passwd")
        self.assertEqual(b["unresolved"][0]["error"], "path escapes the wiki root")

    def test_sections_are_sanitized_like_memories(self):
        (self.root / "sources" / "hosts.md").write_text("reach 7.150.10.239 to start\n")
        b = self.bundle("sources/hosts.md")
        self.assertIn("<HOST-1>", b["source_sections"][0]["text"])

    def test_memories_and_sections_travel_in_one_bundle(self):
        execute = make_execute({MEM_A: TEXT_A})
        b, _ = build_bundle([MEM_A, "sources/doc.md#Other"], execute, wiki_root=self.root)
        self.assertEqual((b["memory_count"], b["source_section_count"]), (1, 1))


class BuildBundleTest(unittest.TestCase):
    def setUp(self):
        self.texts = {MEM_A: TEXT_A, MEM_B: TEXT_B}

    def test_hash_is_of_original_text_not_sanitized(self):
        bundle, _ = build_bundle([MEM_A], make_execute(self.texts))
        self.assertEqual(bundle["memories"][0]["sha256"], sha256_text(TEXT_A))

    def test_mapping_is_not_inside_the_bundle(self):
        bundle, mapping = build_bundle([MEM_A], make_execute(self.texts))
        self.assertNotIn("7.150.10.239", str(bundle))
        self.assertIn("7.150.10.239", str(mapping))

    def test_unresolved_ids_are_reported_not_raised(self):
        bundle, _ = build_bundle([MEM_A, "missing"], make_execute(self.texts))
        self.assertEqual(bundle["memory_count"], 1)
        self.assertEqual(bundle["unresolved"][0]["id"], "missing")

    def test_duplicate_ids_are_collapsed(self):
        bundle, _ = build_bundle([MEM_A, MEM_A], make_execute(self.texts))
        self.assertEqual(bundle["memory_count"], 1)

    def test_supersession_is_recorded(self):
        execute = make_execute(self.texts, head={"heads": [MEM_B]})
        bundle, _ = build_bundle([MEM_A], execute)
        self.assertTrue(bundle["memories"][0]["superseded"])

    def test_no_sanitize_leaves_text_intact(self):
        bundle, _ = build_bundle([MEM_A], make_execute(self.texts), sanitize=False)
        self.assertIn("7.150.10.239", bundle["memories"][0]["text"])


if __name__ == "__main__":
    unittest.main()
