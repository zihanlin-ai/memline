"""Unit tests for the wiki batch planner.

The properties that matter: a session is never silently split across batches
without being marked, a batch never exceeds the ceiling, small sessions travel
together but keep their own identity, ledger memories are never mistaken for
sessions, and the same store always yields the same plan.
"""

from __future__ import annotations

import unittest

from memline.wiki.batch import LEDGER_SOURCE, plan_batches, plan_summary


def mem(mid, session=None, day=1, source="codex"):
    meta = {"source": source}
    if session:
        meta["session_id"] = session
    return {"id": mid, "created_at": f"2026-07-{day:02d}T10:00:00", "metadata": meta}


def store(spec, source="codex"):
    """spec: {session_id: (count, day)}"""
    out = []
    for session, (count, day) in spec.items():
        out += [mem(f"{session}-{i}", session, day, source) for i in range(count)]
    return out


class PlanTest(unittest.TestCase):
    def test_large_session_is_split_into_marked_parts(self):
        batches = plan_batches(store({"s1": (300, 1)}), max_memories=100)
        self.assertEqual({b["kind"] for b in batches}, {"session-part"})
        self.assertEqual([b["part"] for b in batches], [1, 2, 3])
        self.assertTrue(all(b["part_count"] == 3 for b in batches))

    def test_no_batch_exceeds_the_ceiling(self):
        spec = {f"s{i}": (40, i + 1) for i in range(10)}
        batches = plan_batches(store(spec), max_memories=100, pack_threshold=60)
        self.assertTrue(all(b["memory_count"] <= 100 for b in batches))

    def test_small_sessions_pack_but_keep_their_identity(self):
        batches = plan_batches(store({"a": (10, 1), "b": (10, 2)}),
                               max_memories=100, pack_threshold=60)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["kind"], "pack")
        self.assertEqual([s["session_id"] for s in batches[0]["sessions"]], ["a", "b"])

    def test_a_session_at_the_threshold_travels_alone(self):
        batches = plan_batches(store({"a": (10, 1), "big": (60, 2), "b": (10, 3)}),
                               max_memories=275, pack_threshold=60)
        kinds = [(b["kind"], b["session_ids"]) for b in batches]
        self.assertIn(("session", ["big"]), kinds)

    def test_ledger_memories_are_not_treated_as_sessions(self):
        memories = store({"s": (5, 1)}) + [
            mem(f"L{i}", session="ses_bulk", day=2, source=LEDGER_SOURCE) for i in range(10)
        ]
        batches = plan_batches(memories, max_memories=100)
        ledger = [b for b in batches if b["kind"] == "ledger"]
        self.assertEqual(sum(b["memory_count"] for b in ledger), 10)
        self.assertEqual(ledger[0]["session_ids"], [])

    def test_memory_without_session_falls_to_ledger(self):
        batches = plan_batches([mem("x", session=None)], max_memories=100)
        self.assertEqual(batches[0]["kind"], "ledger")

    def test_plan_is_deterministic(self):
        memories = store({f"s{i}": (i + 1, i + 1) for i in range(12)})
        first = plan_batches(memories, max_memories=20, pack_threshold=15)
        second = plan_batches(list(reversed(memories)), max_memories=20, pack_threshold=15)
        self.assertEqual(first, second)

    def test_every_memory_lands_in_exactly_one_batch(self):
        memories = store({f"s{i}": (i * 3 + 1, i + 1) for i in range(8)})
        memories += [mem(f"L{i}", None, 9, LEDGER_SOURCE) for i in range(25)]
        batches = plan_batches(memories, max_memories=30, pack_threshold=20)
        placed = [mid for b in batches for mid in b["memory_ids"]]
        self.assertEqual(sorted(placed), sorted(m["id"] for m in memories))

    def test_summary_counts_sessions_and_ledger_apart(self):
        memories = store({"a": (5, 1)}) + [mem("L1", None, 2, LEDGER_SOURCE)]
        summary = plan_summary(plan_batches(memories, max_memories=100))
        self.assertEqual(summary["memories_in_sessions"], 5)
        self.assertEqual(summary["memories_in_ledger"], 1)
        self.assertEqual(summary["sessions"], 1)



class IncrementalTest(unittest.TestCase):
    """A session that gained memories is re-read whole, not incrementally."""

    def setUp(self):
        self.memories = (
            store({"old": (5, 1)})                      # untouched since the cursor
            + store({"grew": (5, 1)})                   # profiled before, then continued
            + [mem("grew-new", "grew", day=9)]
            + store({"fresh": (3, 9)})                  # entirely new
        )

    def selected(self, since="2026-07-05"):
        from memline.wiki.batch import select_since
        return {m["id"] for m in select_since(self.memories, since)}

    def test_a_session_that_gained_a_memory_comes_back_whole(self):
        picked = self.selected()
        self.assertIn("grew-new", picked)
        self.assertTrue(all(f"grew-{i}" in picked for i in range(5)),
                        "the session's earlier memories must be re-read too")

    def test_an_untouched_session_is_left_alone(self):
        self.assertFalse(any(mid.startswith("old-") for mid in self.selected()))

    def test_a_new_session_is_included(self):
        self.assertTrue(all(f"fresh-{i}" in self.selected() for i in range(3)))

    def test_updated_at_counts_as_movement(self):
        from memline.wiki.batch import select_since
        edited = [mem("old-0", "old", day=1)]
        edited[0]["updated_at"] = "2026-07-09T00:00:00"
        self.assertEqual([m["id"] for m in select_since(edited, "2026-07-05")], ["old-0"])

    def test_ledger_memories_are_taken_individually(self):
        from memline.wiki.batch import select_since
        old = mem("L-old", None, day=1, source=LEDGER_SOURCE)
        new = mem("L-new", None, day=9, source=LEDGER_SOURCE)
        self.assertEqual([m["id"] for m in select_since([old, new], "2026-07-05")], ["L-new"])

    def test_plan_since_batches_only_the_selection(self):
        batches = plan_batches(self.memories, since="2026-07-05", max_memories=100)
        placed = {mid for b in batches for mid in b["memory_ids"]}
        self.assertEqual(placed, self.selected())

if __name__ == "__main__":
    unittest.main()
