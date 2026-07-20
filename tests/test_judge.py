"""Tests for the staleness judge's output parsing (truncation salvage etc.)."""

from __future__ import annotations

import json
import unittest

from mem0_local.judge import build_user_message, parse_judgments


def j(id_: str, verdict: str = "SUPERSEDED", confidence: float = 0.9, reason: str = "r") -> dict:
    return {"id": id_, "verdict": verdict, "confidence": confidence, "reason": reason}


class ParseJudgmentsTests(unittest.TestCase):
    def test_parses_valid_json(self) -> None:
        raw = json.dumps({"judgments": [j("a"), j("b", "KEPT", 0.4)]})
        out = parse_judgments(raw, {"a", "b"})
        self.assertEqual([o["id"] for o in out], ["a", "b"])
        self.assertEqual(out[1]["verdict"], "KEPT")

    def test_strips_code_fences(self) -> None:
        raw = "```json\n" + json.dumps({"judgments": [j("a")]}) + "\n```"
        out = parse_judgments(raw, {"a"})
        self.assertEqual(len(out), 1)

    def test_salvages_truncated_output(self) -> None:
        # max_tokens cut the batch mid-object: complete objects still count.
        raw = (
            '{"judgments":[{"id":"a","verdict":"SUPERSEDED","confidence":0.8,"reason":"x"},'
            '{"id":"b","verdict":"KEPT","confidence":0.7,"reason":"y"},'
            '{"id":"c","verdict":"SUPER'
        )
        out = parse_judgments(raw, {"a", "b", "c"})
        self.assertEqual([o["id"] for o in out], ["a", "b"])

    def test_drops_hallucinated_ids_and_invalid_verdicts(self) -> None:
        raw = json.dumps(
            {"judgments": [j("a"), j("ghost"), j("b", "MAYBE"), {"not": "a judgment"}]}
        )
        out = parse_judgments(raw, {"a", "b"})
        self.assertEqual([o["id"] for o in out], ["a"])

    def test_verdict_case_is_normalized(self) -> None:
        out = parse_judgments(json.dumps({"judgments": [j("a", "superseded")]}), {"a"})
        self.assertEqual(out[0]["verdict"], "SUPERSEDED")

    def test_confidence_clamped_and_defaulted(self) -> None:
        raw = json.dumps(
            {
                "judgments": [
                    j("a", confidence=1.7),
                    j("b", confidence=-0.2),
                    {"id": "c", "verdict": "KEPT", "confidence": "bad", "reason": "r"},
                ]
            }
        )
        out = parse_judgments(raw, {"a", "b", "c"})
        self.assertEqual([o["confidence"] for o in out], [1.0, 0.0, 0.0])

    def test_reason_truncated_to_500_chars(self) -> None:
        out = parse_judgments(
            json.dumps({"judgments": [j("a", reason="x" * 900)]}), {"a"}
        )
        self.assertEqual(len(out[0]["reason"]), 500)

    def test_empty_and_garbage_responses_raise(self) -> None:
        with self.assertRaises(ValueError):
            parse_judgments(None, {"a"})
        with self.assertRaises(ValueError):
            parse_judgments("", {"a"})
        with self.assertRaises(ValueError):
            parse_judgments("total nonsense with no objects", {"a"})

    def test_missing_judgments_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_judgments(json.dumps({"something": []}), {"a"})


class ParseSingleJudgmentTests(unittest.TestCase):
    def test_valid_necessity_verdict(self) -> None:
        from mem0_local.judge import NECESSITY_VERDICTS, parse_single_judgment

        out = parse_single_judgment(
            json.dumps({"verdict": "progress_tick", "confidence": 0.9, "reason": "r"}),
            NECESSITY_VERDICTS,
            default_verdict="DURABLE",
        )
        self.assertEqual(out["verdict"], "PROGRESS_TICK")

    def test_unknown_verdict_falls_back_to_default(self) -> None:
        from mem0_local.judge import NECESSITY_VERDICTS, parse_single_judgment

        out = parse_single_judgment(
            json.dumps({"verdict": "BANANA", "confidence": 0.9, "reason": "r"}),
            NECESSITY_VERDICTS,
            default_verdict="DURABLE",
        )
        self.assertEqual(out["verdict"], "DURABLE")

    def test_truncated_output_salvages_verdict(self) -> None:
        from mem0_local.judge import TIMESTAMP_VERDICTS, parse_single_judgment

        out = parse_single_judgment(
            '{"verdict":"TIMESTAMP_SUSPECT","confidence":0.8,"reason":"cut of',
            TIMESTAMP_VERDICTS,
            default_verdict="CONSISTENT",
        )
        self.assertEqual(out["verdict"], "TIMESTAMP_SUSPECT")

    def test_confidence_clamped_and_empty_raises(self) -> None:
        from mem0_local.judge import NECESSITY_VERDICTS, parse_single_judgment

        out = parse_single_judgment(
            json.dumps({"verdict": "DURABLE", "confidence": 3.0, "reason": "r"}),
            NECESSITY_VERDICTS,
            default_verdict="DURABLE",
        )
        self.assertEqual(out["confidence"], 1.0)
        with self.assertRaises(ValueError):
            parse_single_judgment(None, NECESSITY_VERDICTS, default_verdict="DURABLE")
        with self.assertRaises(ValueError):
            parse_single_judgment("garbage", NECESSITY_VERDICTS, default_verdict="DURABLE")


class BuildUserMessageTests(unittest.TestCase):
    def test_contains_new_entry_and_candidates(self) -> None:
        msg = build_user_message(
            {"id": "n", "text": "new fact", "date": "2026-07-20"},
            [{"id": "o", "text": "old fact", "date": "2026-07-01"}],
        )
        self.assertIn("2026-07-20", msg)
        self.assertIn("new fact", msg)
        self.assertIn('"id": "o"', msg)
        self.assertIn("old fact", msg)


if __name__ == "__main__":
    unittest.main()
