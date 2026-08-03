"""`update` must re-judge the text it just wrote.

Suspicion pairs are keyed on the judged text's hash, so an update expires every
flag standing against that memory. Before 2026-07-28 nothing re-armed them: a
correctness rewrite that left the defect in place (e.g. a LANGUAGE_SUSPECT fix
that kept Chinese narration) closed its own flag and escaped review for good.
"""

from __future__ import annotations

import contextlib
import unittest
import unittest.mock
from typing import Any

from memline import cli


class _Span:
    def __init__(self) -> None:
        self.result: Any = {"id": "m1"}


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, op: str, args: dict) -> str:
        self.enqueued.append((op, args))
        return "evt-1"


class UpdateRejudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = _FakeQueue()
        self.calls: list[tuple[str, dict]] = []

        def fake_execute(op: str, args: dict) -> Any:
            self.calls.append((op, args))
            if op == "get":
                return {"id": "m1", "memory": "old text", "metadata": {},
                        "created_at": "2026-07-20T01:00:00+00:00",
                        "user_id": "workspace", "agent_id": "claude", "run_id": "s1"}
            return {"id": "m1", "updated": True}

        @contextlib.contextmanager
        def fake_audited(*_a: Any, **_k: Any):
            yield _Span()

        self._patches = [
            (cli._support, "execute", fake_execute),
            (cli._support, "audited", fake_audited),
            (cli._support, "output", lambda *a, **k: None),
        ]
        self._saved = [(obj, name, getattr(obj, name)) for obj, name, _ in self._patches]
        for obj, name, new in self._patches:
            setattr(obj, name, new)

        import memline.queue as queue_mod

        self._saved_queue = queue_mod.EventQueue
        queue_mod.EventQueue = lambda *a, **k: self.queue  # type: ignore[assignment]

    def tearDown(self) -> None:
        for obj, name, old in self._saved:
            setattr(obj, name, old)
        import memline.queue as queue_mod

        queue_mod.EventQueue = self._saved_queue  # type: ignore[assignment]

    def _run_update(self) -> None:
        cli.update("m1", "rewritten text", metadata=[], json_flag=False, output_format="quiet")

    def test_update_enqueues_a_self_only_recheck(self) -> None:
        self._run_update()
        self.assertEqual(len(self.queue.enqueued), 1)
        op, args = self.queue.enqueued[0]
        self.assertEqual(op, "stale_check")
        self.assertEqual(args["new_id"], "m1")
        # Self-checks only: re-examine this entry, do not re-scan its neighbors.
        self.assertTrue(args["self_only"])

    def test_enqueue_failure_never_breaks_the_update(self) -> None:
        """The write is already committed and audited by then; a judging
        hiccup must not surface as a failed update."""
        import memline.queue as queue_mod

        def boom(*_a: Any, **_k: Any):
            raise RuntimeError("queue unavailable")

        queue_mod.EventQueue = boom  # type: ignore[assignment]
        self._run_update()  # must not raise
        self.assertIn(("update", {"memory_id": "m1", "text": "rewritten text",
                                  "metadata": unittest.mock.ANY}),
                      [(op, a) for op, a in self.calls if op == "update"])


if __name__ == "__main__":
    unittest.main()
