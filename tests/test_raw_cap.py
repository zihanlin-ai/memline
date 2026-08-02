"""Hard cap on raw (verbatim) write length.

Raw entries are atomic single facts; the cap rejects long dumps at the CLI
seam and tells the agent to split. Updates may keep or shrink an over-cap
legacy entry (redaction must never be blocked) but cannot grow past the cap.
"""

from __future__ import annotations

import unittest

import click

from memline import cli
from memline.config import MAX_RAW_TEXT_CHARS


class RawCapTests(unittest.TestCase):
    def test_under_and_at_cap_pass(self) -> None:
        cli.check_raw_length("x" * (MAX_RAW_TEXT_CHARS - 1))
        cli.check_raw_length("x" * MAX_RAW_TEXT_CHARS)

    def test_over_cap_add_is_rejected_with_split_hint(self) -> None:
        with self.assertRaises(click.ClickException) as ctx:
            cli.check_raw_length("x" * (MAX_RAW_TEXT_CHARS + 1))
        msg = str(ctx.exception.message)
        self.assertIn("hard cap", msg)
        self.assertIn("Split", msg)
        self.assertIn("--infer", msg)

    def test_non_string_content_is_exempt(self) -> None:
        # Extraction inputs (message lists) are not raw verbatim writes.
        cli.check_raw_length([{"role": "user", "content": "x" * 99999}])
        cli.check_raw_length(None)

    def test_update_may_shrink_or_keep_an_over_cap_legacy_entry(self) -> None:
        legacy = "x" * (MAX_RAW_TEXT_CHARS + 500)
        cli.check_raw_length(legacy[:-10], previous=legacy)  # redaction shrink
        cli.check_raw_length(legacy, previous=legacy)  # same-length edit

    def test_update_cannot_grow_past_the_cap(self) -> None:
        legacy = "x" * (MAX_RAW_TEXT_CHARS + 500)
        with self.assertRaises(click.ClickException):
            cli.check_raw_length(legacy + "y", previous=legacy)
        with self.assertRaises(click.ClickException):
            cli.check_raw_length("x" * (MAX_RAW_TEXT_CHARS + 1), previous="short")


if __name__ == "__main__":
    unittest.main()
