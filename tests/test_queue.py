"""Tests for the persistent async-add event queue."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memline.queue import EventQueue, MAX_ATTEMPTS


class EventQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = EventQueue(Path(self._tmp.name) / "queue.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _age(self, event_id: str, updated_at: str) -> None:
        self.queue._conn.execute(
            "UPDATE events SET updated_at = ? WHERE id = ?", (updated_at, event_id)
        )
        self.queue._conn.commit()

    def test_enqueue_claim_complete_roundtrip(self) -> None:
        event_id = self.queue.enqueue("add", {"content": "fact", "infer": True})
        item = self.queue.claim_next()
        self.assertEqual(item["id"], event_id)
        self.assertEqual(item["attempts"], 1)
        self.assertIsNone(self.queue.claim_next())

        self.queue.complete(event_id, {"results": [{"id": "m1"}]})
        row = self.queue.get(event_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result"]["results"][0]["id"], "m1")

    def test_fail_requeues_until_max_attempts_then_terminal(self) -> None:
        event_id = self.queue.enqueue("add", {"content": "flaky"})
        for attempt in range(1, MAX_ATTEMPTS + 1):
            item = self.queue.claim_next()
            self.assertIsNotNone(item, f"attempt {attempt} should be claimable")
            terminal = self.queue.fail(event_id, "boom", item["attempts"])
            self.assertEqual(terminal, attempt == MAX_ATTEMPTS)
        self.assertEqual(self.queue.get(event_id)["status"], "failed")
        self.assertIsNone(self.queue.claim_next())

    def test_retry_resets_failed_event(self) -> None:
        event_id = self.queue.enqueue("add", {"content": "x"})
        self.queue.claim_next()
        for _ in range(MAX_ATTEMPTS - 1):
            self.queue.fail(event_id, "boom", MAX_ATTEMPTS)
            break
        self.queue.fail(event_id, "boom", MAX_ATTEMPTS)
        self.assertTrue(self.queue.retry(event_id))
        row = self.queue.get(event_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertFalse(self.queue.retry(event_id))  # only failed rows retry

    def test_ack_clears_alert_state(self) -> None:
        event_id = self.queue.enqueue("add", {"content": "x"})
        self.queue.claim_next()
        self.queue.fail(event_id, "boom", MAX_ATTEMPTS)
        self.assertEqual(self.queue.ack(None), 1)
        self.assertTrue(self.queue.get(event_id)["acked"])
        self.assertEqual(self.queue.ack(None), 0)

    def test_recover_stale_requeues_processing_rows(self) -> None:
        event_id = self.queue.enqueue("add", {"content": "x"})
        self.queue.claim_next()
        self.assertEqual(self.queue.get(event_id)["status"], "processing")
        self.assertEqual(self.queue.recover_stale(), 1)
        self.assertEqual(self.queue.get(event_id)["status"], "queued")

    def test_purge_drops_old_terminal_rows_keeps_unacked_failures(self) -> None:
        old_done = self.queue.enqueue("add", {"content": "old done"})
        self.queue.complete(old_done, {})
        self._age(old_done, "2026-06-01T00:00:00+00:00")

        old_unacked = self.queue.enqueue("add", {"content": "old unacked failure"})
        self.queue.claim_next()  # old_unacked? claim order is created_at; both aged below
        self.queue.fail(old_unacked, "boom", MAX_ATTEMPTS)
        self._age(old_unacked, "2026-06-01T00:00:00+00:00")

        fresh = self.queue.enqueue("add", {"content": "fresh"})
        self.queue.complete(fresh, {})

        self.assertEqual(self.queue.purge(30), 1)
        remaining = {row["event_id"] for row in self.queue.list(limit=10)}
        self.assertNotIn(old_done, remaining)
        self.assertIn(old_unacked, remaining)
        self.assertIn(fresh, remaining)


if __name__ == "__main__":
    unittest.main()
