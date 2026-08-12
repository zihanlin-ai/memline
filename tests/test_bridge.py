from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memline import bridge


class BridgeQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bridge.db"
        self.queue = bridge.BridgeQueue(self.db_path)
        self.queue.mark_ready("daemon-a", 123)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_round_trip_preserves_payload_and_response(self):
        payload = {"op": "search", "args": {"query": "hello", "limit": 3}}
        request_id = self.queue.enqueue(payload)

        item = self.queue.claim_next()
        self.assertEqual(item["payload"], payload)
        self.queue.complete(item["id"], {"status": "ok", "result": {"ids": ["m1"]}})

        response = self.queue.response(request_id)
        self.assertEqual(response["result"], {"ids": ["m1"]})
        self.assertEqual(response["_bridge_status"], "done")
        self.queue.acknowledge(request_id)
        self.assertIsNone(self.queue.response(request_id))

    def test_processing_rows_fail_safely_after_daemon_restart(self):
        request_id = self.queue.enqueue({"op": "ping"})
        self.assertEqual(self.queue.claim_next()["id"], request_id)

        self.assertEqual(self.queue.recover_stale(), 1)
        response = self.queue.response(request_id)
        self.assertEqual(response["_bridge_status"], "error")
        self.assertIn("outcome is unknown", response["error"])
        self.assertIsNone(self.queue.claim_next())

    def test_cancel_only_prevents_work_that_was_not_claimed(self):
        queued_id = self.queue.enqueue({"op": "ping"})
        self.assertTrue(self.queue.cancel_if_queued(queued_id, "client timed out"))
        self.assertIsNone(self.queue.claim_next())

        processing_id = self.queue.enqueue({"op": "ping"})
        self.assertEqual(self.queue.claim_next()["id"], processing_id)
        self.assertFalse(self.queue.cancel_if_queued(processing_id, "client timed out"))

    def test_stale_heartbeat_is_not_ready(self):
        stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        with self.queue._lock:
            self.queue._conn.execute(
                "UPDATE bridge_state SET heartbeat_at = ? WHERE singleton = 1", (stale,)
            )
            self.queue._conn.commit()

        self.assertFalse(self.queue.is_ready())

    def test_request_waits_for_worker_and_returns_same_handler_result(self):
        stop = threading.Event()

        def worker() -> None:
            worker_queue = bridge.BridgeQueue(self.db_path)
            while not stop.is_set():
                item = worker_queue.claim_next()
                if item is None:
                    time.sleep(0.005)
                    continue
                worker_queue.complete(
                    item["id"],
                    {"status": "ok", "result": {"echo": item["payload"]}},
                )
                return

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            payload = {"op": "get", "args": {"memory_id": "m1"}}
            result = bridge.request(payload, timeout=2, db_path=self.db_path)
        finally:
            stop.set()
            thread.join(timeout=2)

        self.assertEqual(result, {"echo": payload})

    def test_concurrent_clients_are_claimed_exactly_once(self):
        count = 24
        ids = [self.queue.enqueue({"op": "ping", "args": {"index": i}}) for i in range(count)]
        claimed: list[str] = []
        claimed_lock = threading.Lock()

        def worker() -> None:
            worker_queue = bridge.BridgeQueue(self.db_path)
            while True:
                item = worker_queue.claim_next()
                if item is None:
                    return
                with claimed_lock:
                    claimed.append(item["id"])
                worker_queue.complete(item["id"], {"status": "ok", "result": {}})

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(sorted(claimed), sorted(ids))
        self.assertEqual(len(claimed), len(set(claimed)))


if __name__ == "__main__":
    unittest.main()
