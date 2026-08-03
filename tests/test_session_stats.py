from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from memline import audit, cli, session_stats
from memline.session_stats import SessionStatsStore


ADD_RESULT = {"results": [{"id": "m-1", "memory": "fact", "event": "ADD"}]}


class SessionStatsStoreTests(unittest.TestCase):
    def test_record_add_increments_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStatsStore(Path(tmp) / "session-stats.db")
            self.assertEqual(store.add_count("sess-a"), 0)
            self.assertEqual(store.record_add("sess-a", "2026-07-27T00:00:00+00:00"), 1)
            self.assertEqual(store.record_add("sess-a", "2026-07-27T00:00:01+00:00"), 2)
            self.assertEqual(store.record_add("sess-b", "2026-07-27T00:00:02+00:00"), 1)
            self.assertEqual(store.add_count("sess-a"), 2)
            self.assertEqual(store.add_count("sess-b"), 1)


class AuditCounterHookTests(unittest.TestCase):
    def append(self, tmp: Path, store: SessionStatsStore, *, operation="add", metadata=None, result=ADD_RESULT):
        with (
            patch.object(audit, "MANIFEST_DIR", tmp / "manifests"),
            patch.object(audit, "MANIFEST_LOCK", tmp / "store" / "manifest.lock"),
            patch.object(session_stats, "session_stats_store", lambda path=None: store),
        ):
            audit.append_live_audit(
                operation=operation,
                input_payload={"content": "x"},
                metadata=metadata,
                result=result,
                started_at="2026-07-27T00:00:00+00:00",
                finished_at="2026-07-27T00:00:01+00:00",
                duration_ms=1000,
                scope={},
            )

    def test_add_row_increments_session_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStatsStore(Path(tmp) / "stats.db")
            meta = {"session_id": "sess-1", "origin": "live_agent"}
            self.append(Path(tmp), store, metadata=meta)
            self.append(Path(tmp), store, metadata=meta)
            self.assertEqual(store.add_count("sess-1"), 2)

    def test_errors_imports_and_non_adds_are_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStatsStore(Path(tmp) / "stats.db")
            self.append(Path(tmp), store, metadata={"session_id": "sess-1"}, result={"error": "boom"})
            self.append(Path(tmp), store, metadata={"session_id": "sess-1", "origin": "ledger_import"})
            self.append(Path(tmp), store, operation="update", metadata={"session_id": "sess-1"})
            self.append(Path(tmp), store, metadata={})
            self.assertEqual(store.add_count("sess-1"), 0)


class HandoffBannerTests(unittest.TestCase):
    def run_main(
        self,
        count: int,
        threshold: int = 200,
        session_id: str | None = "sess-banner",
        subcommand: str = "add",
        stale_open: int = 0,
    ):
        store = MagicMock()
        store.add_count.return_value = count
        context = {"session_id": session_id} if session_id else {}
        console = MagicMock()
        with (
            patch.object(cli._support, "detect_writer_context", return_value=context),
            patch("memline.queue.read_alerts", return_value={}),
            patch("memline.staleness.pair_store", return_value=MagicMock(open_count=MagicMock(return_value=stale_open))),
            patch("memline.config.SESSION_ADD_ALERT_THRESHOLD", threshold),
            patch.object(session_stats, "session_stats_store", lambda path=None: store),
            patch.object(cli._support, "err_console", console),
        ):
            cli.main(ctx=MagicMock(invoked_subcommand=subcommand), json_output=False)
        return console

    def banner_lines(self, console: MagicMock, marker: str = "accumulated") -> list[str]:
        return [str(call.args[0]) for call in console.print.call_args_list if marker in str(call.args[0])]

    def test_banner_fires_over_threshold(self):
        console = self.run_main(201)
        lines = self.banner_lines(console)
        self.assertEqual(len(lines), 1)
        self.assertIn("201", lines[0])
        self.assertIn("handoff", lines[0])

    def test_no_banner_at_or_under_threshold(self):
        self.assertEqual(self.banner_lines(self.run_main(200)), [])

    def test_no_banner_without_session_or_when_disabled(self):
        self.assertEqual(self.banner_lines(self.run_main(500, session_id=None)), [])
        self.assertEqual(self.banner_lines(self.run_main(500, threshold=0)), [])

    def test_hygiene_banners_gated_to_review_commands(self):
        for subcommand, expected in (("add", 0), ("search", 0), ("stale", 1), ("review", 1), ("status", 1)):
            console = self.run_main(0, subcommand=subcommand, stale_open=3)
            self.assertEqual(
                len(self.banner_lines(console, marker="staleness suspicion")), expected, subcommand
            )

    def test_handoff_banner_shows_on_routine_commands(self):
        self.assertEqual(len(self.banner_lines(self.run_main(201, subcommand="search"))), 1)


if __name__ == "__main__":
    unittest.main()
