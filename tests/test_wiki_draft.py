"""What must be true before a draft is generated, and kept after it is.

Generation happens on an endpoint outside this machine, so every test here is
about the boundary rather than the prose. Three things have to hold:

*Nothing unreviewed leaves.* The first run of this pipeline sent two real
names and a set of addresses to a third party because the sanitizer merely
*reported* what it could not judge. The refusal has to happen before the call,
not be checked in the result, and that ordering is what most of these tests
pin down.

*A ruling is durable.* Paying for review once and re-paying every run are the
difference between a usable pipeline and an abandoned one, so both kinds of
verdict — redact this, this one is fine — have to survive.

*The bundle is kept beside the draft.* An argument about a sentence is settled
by reading what the writer was given. If the sidecar is missing or is not the
material that was actually sent, there is nothing to settle it with.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from memline.wiki.draft import (
    UnreviewedMaterialError,
    draft_topic,
    load_review,
    material_of,
    render,
)

MEM_A = "aaaa1111-2222-3333-4444-555566667777"
MEM_B = "bbbb1111-2222-3333-4444-555566667777"


@dataclass
class FakeResult:
    model: str = "test-model"
    endpoint: str = "test-endpoint"
    attempt: int = 1
    seconds: float = 1.0
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 10, "completion_tokens": 20})
    earlier_failures: list = field(default_factory=list)

    @property
    def provenance(self) -> dict:
        return {"endpoint": self.endpoint, "model": self.model}


def store(**texts):
    """An ``execute`` over a fixed set of memories; ids not listed do not exist."""
    heads = {}

    def execute(op, args):
        mid = args["memory_id"]
        if op == "get":
            if mid not in texts:
                raise RuntimeError("no such memory")
            return {"memory": texts[mid], "created_at": "2026-01-02T03:04:05Z",
                    "metadata": {"writer_agent_id": "claude"}}
        if op == "resolve_head":
            return {"heads": heads.get(mid, [mid])}
        raise AssertionError(op)

    execute.heads = heads  # type: ignore[attr-defined]
    return execute


def full_draft():
    return {
        "title": "A title",
        "summary": "What this establishes.",
        "article_markdown": "Body text.",
        "claims": [{"id": "c1"}],
        "open_questions": ["one"],
        "unused_evidence_refs": [],
    }


class DraftTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "drafts"
        self.addCleanup(self._tmp.cleanup)

    def topic(self, refs=(f"mem:{MEM_A}",), **extra):
        return {"id": "t1", "topic_key": "a-topic", "title": "A title",
                "evidence": [{"ref": r} for r in refs], **extra}

    def run_draft(self, execute, *, reply=None, review_file=None, topic=None):
        """Draft with the endpoint stubbed. Returns ``(summary, prompt_sent)``."""
        sent = {}

        def call_json(prompt, **kwargs):
            sent["prompt"] = prompt
            sent["kwargs"] = kwargs
            return (full_draft() if reply is None else reply), FakeResult()

        with mock.patch("memline.wiki.draft.call_json", call_json):
            summary = draft_topic(topic or self.topic(), execute, "P:{material}",
                                  self.out, wiki_root=self.root,
                                  review_file=review_file, log=lambda _: None)
        return summary, sent

    # --- nothing unreviewed leaves ----------------------------------------

    def test_an_unruled_personal_name_blocks_the_call(self):
        with self.assertRaises(UnreviewedMaterialError) as caught:
            self.run_draft(store(**{MEM_A: "张伟 ran the sweep"}))
        self.assertIn("张伟", str(caught.exception))

    def test_an_unruled_email_blocks_the_call(self):
        with self.assertRaises(UnreviewedMaterialError):
            self.run_draft(store(**{MEM_A: "ask ops@example.com about it"}))

    def test_the_endpoint_is_never_reached_when_material_is_unruled(self):
        # The incident this module exists to prevent: the flags were reported
        # and the call went out anyway. Checking the result is too late.
        called = []

        def call_json(prompt, **kwargs):
            called.append(prompt)
            return full_draft(), FakeResult()

        with mock.patch("memline.wiki.draft.call_json", call_json):
            with self.assertRaises(UnreviewedMaterialError):
                draft_topic(self.topic(), store(**{MEM_A: "张伟 ran it"}), "P:{material}",
                            self.out, wiki_root=self.root, log=lambda _: None)
        self.assertEqual(called, [])

    def test_no_sidecar_survives_a_refused_call(self):
        with self.assertRaises(UnreviewedMaterialError):
            self.run_draft(store(**{MEM_A: "张伟 ran it"}))
        self.assertEqual(sorted(p.name for p in self.out.glob("*.md")), [])

    def test_a_non_blocking_flag_does_not_stop_the_run(self):
        # A long hex id is worth surfacing and is not worth refusing on: it
        # names no one. Refusing on everything trains people to clear
        # everything.
        summary, sent = self.run_draft(store(**{MEM_A: "commit " + "a" * 40}))
        self.assertTrue(sent["prompt"])
        self.assertEqual(summary["topic_key"], "a-topic")

    # --- a ruling is durable ----------------------------------------------

    def test_a_redacted_value_is_replaced_in_what_is_sent(self):
        review = self.root / "review.json"
        review.write_text(json.dumps({"redact": {"张伟": "PERSON"}}), encoding="utf-8")
        _, sent = self.run_draft(store(**{MEM_A: "张伟 ran the sweep"}),
                                 review_file=review)
        self.assertNotIn("张伟", sent["prompt"])

    def test_a_cleared_value_stops_blocking_without_being_replaced(self):
        # "We looked and it is fine" is a decision too, and it has to be as
        # durable as "replace this" or it gets re-made every run.
        review = self.root / "review.json"
        review.write_text(json.dumps({"cleared": ["张伟"]}), encoding="utf-8")
        _, sent = self.run_draft(store(**{MEM_A: "张伟 ran the sweep"}),
                                 review_file=review)
        self.assertIn("张伟", sent["prompt"])

    def test_a_missing_review_file_is_simply_no_rulings(self):
        self.assertEqual(load_review(self.root / "absent.json"), ({}, set()))
        self.assertEqual(load_review(None), ({}, set()))

    # --- retired evidence is surfaced before the call ----------------------

    def test_a_superseded_memory_is_recorded_rather_than_dropped(self):
        execute = store(**{MEM_A: "the old belief"})
        execute.heads[MEM_A] = ["cccc1111-2222-3333-4444-555566667777"]
        summary, _ = self.run_draft(execute)
        self.assertEqual(summary["superseded_evidence"], 1)
        claims = json.loads((self.out / "a-topic.claims.json").read_text())
        self.assertEqual(claims["superseded_evidence"], [MEM_A])

    def test_an_unresolvable_reference_is_counted_not_silently_absorbed(self):
        summary, _ = self.run_draft(store(**{MEM_A: "present"}),
                                    topic=self.topic([f"mem:{MEM_A}", f"mem:{MEM_B}"]))
        self.assertEqual(summary["unresolved_evidence"], 1)
        claims = json.loads((self.out / "a-topic.claims.json").read_text())
        self.assertEqual([e["id"] for e in claims["unresolved_evidence"]], [MEM_B])

    # --- the bundle is kept beside the draft -------------------------------

    def test_the_article_and_all_three_sidecars_are_written(self):
        self.run_draft(store(**{MEM_A: "a fact"}))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["a-topic.bundle.json", "a-topic.claims.json",
                          "a-topic.md", "a-topic.placeholders.json"])

    def test_the_saved_bundle_is_the_material_that_was_sent(self):
        _, sent = self.run_draft(store(**{MEM_A: "a fact"}))
        bundle = json.loads((self.out / "a-topic.bundle.json").read_text())
        self.assertIn(material_of(bundle), sent["prompt"])

    def test_the_placeholder_map_stays_local_and_out_of_the_prompt(self):
        review = self.root / "review.json"
        review.write_text(json.dumps({"redact": {"张伟": "PERSON"}}), encoding="utf-8")
        _, sent = self.run_draft(store(**{MEM_A: "张伟 ran the sweep"}),
                                 review_file=review)
        # The map is placeholder -> original, and it is the originals that
        # must not have travelled. Keeping it beside the draft is what lets a
        # local reader restore them; sending it would defeat the whole scrub.
        mapping = json.loads((self.out / "a-topic.placeholders.json").read_text())
        self.assertTrue(mapping)
        for placeholder, original in mapping.items():
            self.assertNotIn(original, sent["prompt"])
            self.assertIn(placeholder, sent["prompt"])

    def test_generation_provenance_is_recorded_with_the_claims(self):
        self.run_draft(store(**{MEM_A: "a fact"}))
        claims = json.loads((self.out / "a-topic.claims.json").read_text())
        self.assertEqual(claims["generation"]["model"], "test-model")

    # --- an unusable reply is an error, not a published page ---------------

    def test_a_reply_missing_required_fields_is_rejected(self):
        reply = full_draft()
        del reply["claims"]
        with self.assertRaises(ValueError) as caught:
            self.run_draft(store(**{MEM_A: "a fact"}), reply=reply)
        self.assertIn("claims", str(caught.exception))

    def test_an_empty_summary_is_rejected(self):
        # The summary is what a shelf listing shows and what a reviewer reads
        # before opening anything; a blank one is a page nobody can choose.
        reply = full_draft() | {"summary": "   "}
        with self.assertRaises(ValueError):
            self.run_draft(store(**{MEM_A: "a fact"}), reply=reply)

    def test_a_rejected_reply_leaves_no_article_behind(self):
        reply = full_draft() | {"summary": ""}
        with self.assertRaises(ValueError):
            self.run_draft(store(**{MEM_A: "a fact"}), reply=reply)
        self.assertFalse((self.out / "a-topic.md").exists())


class RenderTest(unittest.TestCase):
    def test_every_field_the_prompt_asks_for_is_substituted(self):
        out = render("{title}|{scope}|{evidence_gaps}|{conflicts}|{sensitive}|{material}",
                     {"title": "T", "scope": "S", "evidence_gaps": "G",
                      "conflicts": "C", "sensitive": "X"}, "M")
        self.assertEqual(out, "T|S|G|C|X|M")

    def test_an_absent_field_says_so_rather_than_vanishing(self):
        # A blank reads as "there were none"; the writer has to be able to
        # tell that apart from "nobody recorded any".
        out = render("{scope}|{conflicts}", {"title": "T"}, "M")
        self.assertEqual(out, "(no scope recorded)|none seen")


class MaterialTest(unittest.TestCase):
    def test_the_writer_sees_provenance_beside_every_memory(self):
        material = json.loads(material_of({
            "memories": [{"id": MEM_A, "text": "a fact", "created_at": "2026-01-02T03:04:05Z",
                          "writer": "claude", "superseded": True}],
            "source_sections": [],
        }))
        entry = material["memories"][0]
        self.assertEqual(entry["date"], "2026-01-02")
        self.assertEqual(entry["writer"], "claude")
        self.assertTrue(entry["superseded"])

    def test_the_local_only_hash_does_not_travel(self):
        material = material_of({
            "memories": [{"id": MEM_A, "text": "a fact", "sha256": "deadbeef"}],
            "source_sections": [],
        })
        self.assertNotIn("deadbeef", material)


if __name__ == "__main__":
    unittest.main()
