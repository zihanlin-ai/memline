"""The compile cursor.

Each test pins a way the next run would silently read the wrong material.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mem0_local.wiki_state import EMPTY, boundary_ids, close_run, read_state, source_hashes


def mem(mid, stamp):
    return {"id": mid, "created_at": stamp, "metadata": {"session_id": "s"}}


class StateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "compile.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_missing_cursor_reads_as_an_empty_one(self):
        self.assertEqual(read_state(self.path), EMPTY)

    def test_an_unreadable_cursor_reads_as_a_missing_one(self):
        self.path.write_text("{ not json")
        self.assertEqual(read_state(self.path), EMPTY)

    def test_closing_stamps_the_start_not_the_end(self):
        new = close_run(self.path, started_at="2026-08-01T10:00:00", memories=[])
        self.assertEqual(new["last_compile_at"], "2026-08-01T10:00:00")

    def test_closing_advances_the_run_number(self):
        close_run(self.path, started_at="2026-08-01T10:00:00", memories=[])
        second = close_run(self.path, started_at="2026-08-02T10:00:00", memories=[])
        self.assertEqual(second["next_run"], 3)

    def test_boundary_holds_only_the_ids_stamped_at_the_cursor(self):
        memories = [mem("a", "2026-08-01T10:00:00"), mem("b", "2026-08-01T10:00:00"),
                    mem("c", "2026-08-01T09:59:59")]
        self.assertEqual(boundary_ids(memories, "2026-08-01T10:00:00"), ["a", "b"])

    def test_source_hashes_track_content_not_names(self):
        src = self.dir / "sources"
        src.mkdir()
        (src / "a.md").write_text("one")
        first = source_hashes(src)
        (src / "a.md").write_text("two")
        self.assertNotEqual(first["a.md"], source_hashes(src)["a.md"])

    def test_hidden_files_are_not_designated_sources(self):
        src = self.dir / "sources"
        src.mkdir()
        (src / ".gitkeep").write_text("")
        (src / "a.md").write_text("one")
        self.assertEqual(list(source_hashes(src)), ["a.md"])

    def test_source_hashes_survive_a_close_that_names_no_source_dir(self):
        src = self.dir / "sources"
        src.mkdir()
        (src / "a.md").write_text("one")
        close_run(self.path, started_at="2026-08-01T10:00:00", memories=[], source_dir=src)
        again = close_run(self.path, started_at="2026-08-02T10:00:00", memories=[])
        self.assertIn("a.md", again["source_hashes"])


if __name__ == "__main__":
    unittest.main()
