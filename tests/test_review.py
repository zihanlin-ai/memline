"""The handoff verdict, computed against fakes — no store, no shell.

The workflow contracts (what blocks a pass across a real lifecycle) live in
test_lifecycle_boundaries and keep exercising the CLI path. These tests are
the unit level that sinking the logic bought: each blocker in isolation, the
wait loop against a fake clock, and the fallback guidance for a verdict the
playbook has never heard of.
"""

from __future__ import annotations

import unittest
from typing import Any

from memline.review import VERDICT_PLAYBOOK, flag_suggestion, session_review


class FakeQueue:
    def __init__(self, rows=(), args=None):
        self.rows = list(rows)
        self.args = args or {}

    def list(self, status=None, limit=500):
        if status is None:
            return self.rows
        return [r for r in self.rows if r["status"] == status]

    def get_args(self, event_id):
        return self.args.get(event_id, {})


class FakePairs:
    def __init__(self, by_session=None, ttl=()):
        self.by_session = by_session or {}
        self.ttl = list(ttl)

    def open_pairs(self, session_id=None, kind=None):
        if kind == "ttl_expiry":
            return self.ttl
        return self.by_session.get(session_id, [])


def pair(pid="p1", kind="displacement", verdict="SUPERSEDED"):
    return {"pair_id": pid, "kind": kind, "old_id": "old", "new_id": "new",
            "verdict": verdict, "confidence": 0.9, "reason": "r"}


def run(*, queue=None, pairs=None, wait=False, writes=(), clock=None, sleep=None):
    kwargs: dict[str, Any] = {}
    if clock:
        kwargs["clock"] = clock
    if sleep:
        kwargs["sleep"] = sleep
    return session_review(
        "s1",
        execute=lambda op, args: {"results": list(writes)} if op == "list" else {},
        queue_factory=lambda: queue or FakeQueue(),
        pairs=pairs or FakePairs(),
        user_id="workspace",
        wait=wait,
        **kwargs,
    )


class VerdictTest(unittest.TestCase):
    def test_nothing_outstanding_is_a_pass(self):
        report = run(writes=[{"id": "m1", "memory": "a fact"}])
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["blocking"], [])

    def test_a_displacement_this_session_raised_blocks_it(self):
        report = run(pairs=FakePairs(by_session={"s1": [pair()]}))
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual([b["kind"] for b in report["blocking"]],
                         ["displacement_raised_by_me"])

    def test_another_sessions_pairs_never_block(self):
        # The authority rule: a session disposes only its own writes, or a
        # shared store would make the verdict unreachable.
        report = run(pairs=FakePairs(by_session={"other": [pair()]}))
        self.assertEqual(report["verdict"], "pass")

    def test_ttl_expiries_block_every_session(self):
        # The exception to the exception: granted to everyone, so they are
        # nobody's property and would otherwise be nobody's obligation.
        report = run(pairs=FakePairs(ttl=[pair("t1", kind="ttl_expiry",
                                              verdict="TTL_EXPIRED")]))
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual([b["kind"] for b in report["blocking"]], ["ttl_expired"])

    def test_self_flags_come_out_in_disposition_order(self):
        # safety first: redact before anything else can invalidate and strand
        # the plaintext.
        flags = [pair("pn", kind="necessity", verdict="BORN_UNNECESSARY"),
                 pair("ps", kind="safety", verdict="SECRET_SUSPECT"),
                 pair("pc", kind="correctness", verdict="TIMESTAMP_SUSPECT")]
        report = run(pairs=FakePairs(by_session={"s1": flags}))
        self.assertEqual([f["kind"] for f in report["self_flags"]],
                         ["safety", "correctness", "necessity"])

    def test_a_failed_own_add_blocks(self):
        queue = FakeQueue([{"event_id": "e1", "op": "add", "status": "failed"}],
                          {"e1": {"session_id": "s1"}})
        report = run(queue=queue)
        self.assertEqual([b["kind"] for b in report["blocking"]], ["failed_adds"])

    def test_another_sessions_failed_add_does_not(self):
        queue = FakeQueue([{"event_id": "e1", "op": "add", "status": "failed"}],
                          {"e1": {"session_id": "other"}})
        self.assertEqual(run(queue=queue)["verdict"], "pass")

    def test_wait_polls_until_the_judgments_land(self):
        queue = FakeQueue([{"event_id": "e1", "op": "stale_check", "status": "queued"}],
                          {"e1": {"session_id": "s1"}})
        ticks = iter(range(100))

        def sleep(_seconds):
            # The judgment lands while we wait.
            queue.rows[0]["status"] = "done"

        report = run(queue=queue, wait=True,
                     clock=lambda: float(next(ticks)), sleep=sleep)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["pending_stale_checks"], 0)

    def test_wait_gives_up_at_the_deadline_and_reports_blocked(self):
        queue = FakeQueue([{"event_id": "e1", "op": "stale_check", "status": "queued"}],
                          {"e1": {"session_id": "s1"}})
        clock_value = [0.0]

        def clock():
            return clock_value[0]

        def sleep(_seconds):
            clock_value[0] += 60.0  # never finishes; deadline must save us

        report = run(queue=queue, wait=True, clock=clock, sleep=sleep)
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual([b["kind"] for b in report["blocking"]],
                         ["pending_stale_checks"])


class SuggestionTest(unittest.TestCase):
    def test_a_known_verdict_carries_its_playbook_entry(self):
        out = flag_suggestion(pair(verdict="SECRET_SUSPECT"))
        self.assertEqual(out["fix"], VERDICT_PLAYBOOK["SECRET_SUSPECT"]["fix"])

    def test_an_unknown_verdict_still_gets_usable_guidance(self):
        # A reviewer handed a bare verdict reconstructs the rule from memory,
        # and the reconstruction is where wrong dispositions come from.
        out = flag_suggestion(pair(verdict="NEVER_SEEN"))
        self.assertIn("fix", out)
        self.assertIn("dismiss_only_if", out)

    def test_dismissal_finality_is_always_stated(self):
        for verdict in ("SECRET_SUSPECT", "NEVER_SEEN"):
            self.assertIn("PERMANENT", flag_suggestion(pair(verdict=verdict))["warning"])


if __name__ == "__main__":
    unittest.main()
