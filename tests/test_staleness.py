"""Tests for supersession semantics (staleness.py) against fake stores."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mem0_local import staleness
from mem0_local.staleness import (
    PairStore,
    StalenessError,
    invalidate,
    resolve_head,
    revive,
    search_with_staleness,
    superseded_ids,
)


class FakeVectorStore:
    """Mimics the vendored qdrant adapter: get -> point, payload-only merge update."""

    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = {k: dict(v) for k, v in payloads.items()}

    def get(self, vector_id):
        payload = self.payloads.get(vector_id)
        if payload is None:
            return None
        return SimpleNamespace(id=vector_id, payload=dict(payload))

    def update(self, vector_id, vector=None, payload=None):
        assert vector is None, "staleness ops must never re-embed"
        self.payloads[vector_id].update(payload or {})


class FakeHistory:
    def __init__(self) -> None:
        self.rows = []

    def add_history(self, memory_id, old, new, event, **kwargs):
        self.rows.append({"memory_id": memory_id, "event": event, **kwargs})


class FakeClient:
    def __init__(self, payloads: dict[str, dict], search_items: list[dict] | None = None):
        self.vector_store = FakeVectorStore(payloads)
        self.db = FakeHistory()
        self.search_calls = []
        self._search_items = search_items or []

    def search(self, query, *, top_k, **kwargs):
        self.search_calls.append({"query": query, "top_k": top_k, **kwargs})
        return {"results": [dict(i) for i in self._search_items[:top_k]]}


def payload_getter(client):
    def _get(mid):
        point = client.vector_store.get(mid)
        if point is None:
            return None
        return dict(point.payload)

    return _get


class StalenessOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Point the process-global pair store at a scratch DB.
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()

    def _client(self, extra: dict[str, dict] | None = None) -> FakeClient:
        payloads = {
            "old": {"data": "probe is 1.0 TPS"},
            "new": {"data": "probe is 2.0 TPS"},
        }
        payloads.update(extra or {})
        return FakeClient(payloads)

    def test_invalidate_sets_fields_and_history(self) -> None:
        client = self._client()
        result = invalidate(client, "old", ["new"], reason="re-ran", actor_id="claude")
        self.assertTrue(result["invalidated"])
        payload = client.vector_store.payloads["old"]
        self.assertEqual(payload["superseded_by"], ["new"])
        self.assertEqual(payload["superseded_reason"], "re-ran")
        self.assertIn("superseded_at", payload)
        # Text untouched.
        self.assertEqual(payload["data"], "probe is 1.0 TPS")
        self.assertEqual(client.db.rows[-1]["event"], "INVALIDATE")

    def test_invalidate_rejects_missing_self_and_double(self) -> None:
        client = self._client()
        with self.assertRaises(StalenessError):
            invalidate(client, "ghost", ["new"])
        with self.assertRaises(StalenessError):
            invalidate(client, "old", ["ghost"])
        with self.assertRaises(StalenessError):
            invalidate(client, "old", ["old"])
        invalidate(client, "old", ["new"])
        with self.assertRaises(StalenessError):
            invalidate(client, "old", ["new"])

    def test_invalidate_refuses_cycle(self) -> None:
        client = self._client({"c": {"data": "v3"}})
        invalidate(client, "old", ["new"])       # old -> new
        invalidate(client, "new", ["c"])         # new -> c
        # c -> old would close the loop old -> new -> c -> old.
        with self.assertRaises(StalenessError):
            invalidate(client, "c", ["old"])
        # But pointing at a mid-chain (already superseded) entry that does not
        # loop back is allowed: the backlog pass records historical edges.
        client2 = self._client({"c": {"data": "v3"}})
        invalidate(client2, "old", ["new"])      # old -> new
        invalidate(client2, "c", ["old"])        # c -> old (chain, no cycle)

    def test_revive_restores_and_requires_invalidated(self) -> None:
        client = self._client()
        invalidate(client, "old", ["new"])
        result = revive(client, "old", actor_id="claude")
        self.assertEqual(result["previous_superseded_by"], ["new"])
        self.assertEqual(superseded_ids(client.vector_store.payloads["old"]), [])
        self.assertEqual(client.db.rows[-1]["event"], "REVIVE")
        with self.assertRaises(StalenessError):
            revive(client, "old")

    def test_invalidate_closes_open_suspicions_on_target(self) -> None:
        client = self._client()
        store = staleness.pair_store()
        store.record_judgment(
            new_id="new", old_id="old", old_text="probe is 1.0 TPS",
            verdict="SUPERSEDED", confidence=0.9, reason="same slot",
        )
        self.assertEqual(store.open_count(), 1)
        result = invalidate(client, "old", ["new"])
        self.assertEqual(result["closed_open_suspicions"], 1)
        self.assertEqual(store.open_count(), 0)

    def test_resolve_head_chain_and_split(self) -> None:
        client = self._client({"c": {"data": "v3"}, "d": {"data": "v3b"}})
        invalidate(client, "old", ["new"])
        invalidate(client, "new", ["c", "d"])   # split
        result = resolve_head(payload_getter(client), "old")
        self.assertEqual(sorted(result["heads"]), ["c", "d"])
        self.assertEqual(result["hops"], 2)
        # An active entry is its own head.
        self.assertEqual(resolve_head(payload_getter(client), "c")["heads"], ["c"])
        with self.assertRaises(StalenessError):
            resolve_head(payload_getter(client), "ghost")


class SearchFilteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()

    def _search(self, client, **kwargs):
        defaults = dict(
            query="q", top_k=2, filters=None, threshold=0.1,
            rerank=False, keyword=False, explain=False,
        )
        defaults.update(kwargs)
        return search_with_staleness(client, **defaults)

    def test_filters_invalidated_and_backfills_from_overfetch(self) -> None:
        items = [
            {"id": "a", "memory": "stale", "metadata": {"superseded_by": ["b"]}},
            {"id": "b", "memory": "current", "metadata": {}},
            {"id": "c", "memory": "also current"},
        ]
        client = FakeClient({}, search_items=items)
        result = self._search(client)
        self.assertEqual([i["id"] for i in result["results"]], ["b", "c"])
        # Over-fetch: asked the backend for more than top_k.
        self.assertGreater(client.search_calls[0]["top_k"], 2)

    def test_include_superseded_returns_raw(self) -> None:
        items = [{"id": "a", "metadata": {"superseded_by": ["b"]}}, {"id": "b"}]
        client = FakeClient({}, search_items=items)
        result = self._search(client, include_superseded=True)
        self.assertEqual([i["id"] for i in result["results"]], ["a", "b"])
        self.assertEqual(client.search_calls[0]["top_k"], 2)

    def test_annotates_suspected_hits(self) -> None:
        staleness.pair_store().record_judgment(
            new_id="n1", old_id="b", old_text="x",
            verdict="SUPERSEDED", confidence=0.8, reason="same slot: probe TPS",
        )
        client = FakeClient({}, search_items=[{"id": "b", "memory": "current"}])
        result = self._search(client, top_k=1)
        hit = result["results"][0]
        self.assertTrue(hit["suspected_stale"])
        self.assertEqual(hit["suspicions"][0]["suspected_by"], "n1")
        self.assertEqual(hit["suspicions"][0]["reason"], "same slot: probe TPS")


class FakeLlm:
    def __init__(self, judgments: list[dict]) -> None:
        self._judgments = judgments
        self.calls = 0

    def generate_response(self, messages, response_format=None):
        self.calls += 1
        import json as _json

        return _json.dumps({"judgments": self._judgments})


class RunStaleCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()

    def _client(self) -> FakeClient:
        items = [
            {"id": "new1", "memory": "probe is 2.0 TPS", "created_at": "2026-07-17"},
            {"id": "old1", "memory": "probe is 1.0 TPS", "created_at": "2026-06-26"},
            {"id": "gone", "memory": "x", "metadata": {"superseded_by": ["y"]}},
            {"id": "pinned", "memory": "method note", "metadata": {"stale_check_pin": True}},
            {"id": "old2", "memory": "unrelated fact", "created_at": "2026-05-01"},
        ]
        client = FakeClient(
            {"new1": {"data": "probe is 2.0 TPS", "user_id": "workspace",
                      "created_at": "2026-07-17T00:00:00+00:00"}},
            search_items=items,
        )
        return client

    def test_judges_filtered_candidates_and_records_pairs(self) -> None:
        client = self._client()
        llm = FakeLlm([
            {"id": "old1", "verdict": "SUPERSEDED", "confidence": 0.9, "reason": "same slot"},
            {"id": "old2", "verdict": "KEPT", "confidence": 0.8, "reason": "different"},
        ])
        result = staleness.run_stale_check(client, "new1", session_id="s1", llm=llm)
        self.assertEqual(result["judged"], 2)
        self.assertEqual(result["opened"], 1)
        store = staleness.pair_store()
        self.assertEqual(store.open_count(), 1)
        open_pair = store.open_pairs(session_id="s1")[0]
        self.assertEqual(open_pair["old_id"], "old1")
        # Self, invalidated, and pinned entries were never sent to the judge.
        self.assertEqual(llm.calls, 1)

    def test_rerun_hits_pair_cache(self) -> None:
        client = self._client()
        llm = FakeLlm([
            {"id": "old1", "verdict": "SUPERSEDED", "confidence": 0.9, "reason": "r"},
            {"id": "old2", "verdict": "KEPT", "confidence": 0.8, "reason": "r"},
        ])
        staleness.run_stale_check(client, "new1", llm=llm)
        again = staleness.run_stale_check(client, "new1", llm=llm)
        self.assertEqual(again["judged"], 0)
        self.assertEqual(again["cached"], 2)
        self.assertEqual(llm.calls, 1)

    def test_judged_either_covers_both_orientations(self) -> None:
        store = staleness.pair_store()
        store.record_judgment(
            new_id="newer", old_id="oldprobe", old_text="v-old",
            verdict="SUPERSEDED", confidence=0.9, reason="",
        )
        # Probing from the other side sees the pair as already judged.
        judged = store.judged_either("oldprobe", "v-old", [("newer", "whatever")])
        self.assertEqual(judged, {"newer"})
        # Unrelated candidate is not covered.
        self.assertEqual(store.judged_either("oldprobe", "v-old", [("x", "t")]), set())

    def test_skips_missing_or_invalidated_new_entry(self) -> None:
        client = self._client()
        self.assertIn("skipped", staleness.run_stale_check(client, "ghost", llm=FakeLlm([])))
        client.vector_store.payloads["new1"]["superseded_by"] = ["z"]
        self.assertIn("skipped", staleness.run_stale_check(client, "new1", llm=FakeLlm([])))


class PairStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_confidence_floor_gates_open(self) -> None:
        low = self.store.record_judgment(
            new_id="n", old_id="o1", old_text="t", verdict="SUPERSEDED",
            confidence=0.5, reason="weak",
        )
        high = self.store.record_judgment(
            new_id="n", old_id="o2", old_text="t", verdict="SUPERSEDED",
            confidence=0.9, reason="strong",
        )
        kept = self.store.record_judgment(
            new_id="n", old_id="o3", old_text="t", verdict="KEPT",
            confidence=0.99, reason="different slot",
        )
        self.assertEqual(low["disposition"], "cached")
        self.assertEqual(high["disposition"], "open")
        self.assertEqual(kept["disposition"], "cached")
        self.assertEqual(self.store.open_count(), 1)

    def test_pair_cache_dedups_same_version_only(self) -> None:
        first = self.store.record_judgment(
            new_id="n", old_id="o", old_text="v1", verdict="KEPT",
            confidence=0.9, reason="",
        )
        rejudged = self.store.record_judgment(
            new_id="n", old_id="o", old_text="v1", verdict="SUPERSEDED",
            confidence=0.9, reason="",
        )
        self.assertTrue(first["inserted"])
        self.assertFalse(rejudged["inserted"])  # same version: never re-judged
        # Text changed -> new version is judgeable again.
        after_update = self.store.record_judgment(
            new_id="n", old_id="o", old_text="v2", verdict="SUPERSEDED",
            confidence=0.9, reason="",
        )
        self.assertTrue(after_update["inserted"])

    def test_reopen_restores_open_disposition(self) -> None:
        row = self.store.record_judgment(
            new_id="n", old_id="o", old_text="t", verdict="SUPERSEDED",
            confidence=0.9, reason="",
        )
        self.assertTrue(self.store.dispose(row["pair_id"], "confirmed", disposed_by="agent"))
        self.assertTrue(self.store.reopen(row["pair_id"]))
        pair = self.store.get(row["pair_id"])
        self.assertEqual(pair["disposition"], "open")
        self.assertIsNone(pair["disposed_by"])
        self.assertIsNone(pair["disposed_at"])
        # Already-open pairs are not "reopened".
        self.assertFalse(self.store.reopen(row["pair_id"]))

    def test_dispose_only_open_pairs(self) -> None:
        row = self.store.record_judgment(
            new_id="n", old_id="o", old_text="t", verdict="SUPERSEDED",
            confidence=0.9, reason="",
        )
        self.assertTrue(self.store.dispose(row["pair_id"], "dismissed", disposed_by="user"))
        self.assertFalse(self.store.dispose(row["pair_id"], "confirmed"))
        with self.assertRaises(StalenessError):
            self.store.dispose(row["pair_id"], "bogus")

    def test_session_scoped_open_pairs(self) -> None:
        self.store.record_judgment(
            new_id="n1", old_id="o1", old_text="t", verdict="SUPERSEDED",
            confidence=0.9, reason="", new_session_id="s1",
        )
        self.store.record_judgment(
            new_id="n2", old_id="o2", old_text="t", verdict="SUPERSEDED",
            confidence=0.9, reason="", new_session_id="s2",
        )
        self.assertEqual(len(self.store.open_pairs()), 2)
        mine = self.store.open_pairs(session_id="s1")
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["new_id"], "n1")


if __name__ == "__main__":
    unittest.main()
