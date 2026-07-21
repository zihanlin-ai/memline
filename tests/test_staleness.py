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
        open_pair = store.open_pairs(session_id="s1", kind="displacement")[0]
        self.assertEqual(open_pair["old_id"], "old1")
        # Self, invalidated, and pinned entries were never sent to the
        # displacement judge; necessity + timestamp + safety self-checks add 3.
        self.assertEqual(llm.calls, 4)

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
        self.assertEqual(again["necessity"], "cached")
        self.assertEqual(again["timestamp"], "cached")
        self.assertEqual(again["safety"], "cached")
        # 4 calls on the first run (necessity, timestamp, safety, displacement);
        # everything version-cached on the rerun.
        self.assertEqual(llm.calls, 4)

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


class RoutingFakeLlm:
    """Answers each judge by recognizing its system prompt."""

    def __init__(self, necessity: dict, timestamp: dict, judgments: list[dict] | None = None,
                 safety: dict | None = None):
        self._necessity = necessity
        self._timestamp = timestamp
        self._safety = safety or {"verdict": "CLEAN", "confidence": 0.9, "reason": "clean"}
        self._judgments = judgments or []
        self.necessity_calls = 0
        self.timestamp_calls = 0
        self.safety_calls = 0
        self.displacement_calls = 0

    def generate_response(self, messages, response_format=None):
        import json as _json

        system = messages[0]["content"]
        if "memory-necessity judge" in system:
            self.necessity_calls += 1
            return _json.dumps(self._necessity)
        if "embedded live credentials" in system:
            self.safety_calls += 1
            return _json.dumps(self._safety)
        if "timestamp or actor" in system:
            self.timestamp_calls += 1
            return _json.dumps(self._timestamp)
        self.displacement_calls += 1
        return _json.dumps({"judgments": self._judgments})


class SelfCheckTests(unittest.TestCase):
    """Necessity (R1) and timestamp (R2) self-checks in run_stale_check."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()

    def _client(self, payload_extra: dict | None = None) -> FakeClient:
        payload = {
            "data": "grid progress: done=38 running=2 pending=22",
            "user_id": "workspace",
            "created_at": "2026-07-20T01:00:00+00:00",
            "ingested_at": "2026-07-20T01:00:00+00:00",
            "source": "claude",
        }
        payload.update(payload_extra or {})
        return FakeClient({"m1": payload}, search_items=[])

    def test_necessity_flag_opens_self_pair_without_candidates(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "EXPIRING", "confidence": 0.9, "reason": "tick"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
        )
        result = staleness.run_stale_check(self._client(), "m1", session_id="s1", llm=llm)
        self.assertEqual(result["necessity"], "EXPIRING")
        self.assertTrue(result["necessity_open"])
        self.assertEqual(result["timestamp"], "CONSISTENT")
        rows = staleness.pair_store().open_pairs(kind="necessity")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["new_id"], "m1")
        self.assertEqual(rows[0]["old_id"], "m1")
        # CONSISTENT timestamp verdict is cached, not opened.
        self.assertEqual(staleness.pair_store().open_pairs(kind="timestamp"), [])

    def test_timestamp_suspect_opens_flag(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "keep"},
            {"verdict": "TIMESTAMP_SUSPECT", "confidence": 0.8, "reason": "date off"},
        )
        result = staleness.run_stale_check(self._client(), "m1", llm=llm)
        self.assertTrue(result["timestamp_open"])
        rows = staleness.pair_store().open_pairs(kind="timestamp")
        self.assertEqual(rows[0]["verdict"], "TIMESTAMP_SUSPECT")

    def test_self_checks_version_cached(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "keep"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
        )
        staleness.run_stale_check(self._client(), "m1", llm=llm)
        again = staleness.run_stale_check(self._client(), "m1", llm=llm)
        self.assertEqual(again["necessity"], "cached")
        self.assertEqual(again["timestamp"], "cached")
        self.assertEqual(again["safety"], "cached")
        self.assertEqual(llm.necessity_calls, 1)
        self.assertEqual(llm.timestamp_calls, 1)
        self.assertEqual(llm.safety_calls, 1)

    def test_safety_flag_opens_on_suspected_secret(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "keep"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
            safety={"verdict": "SECRET_SUSPECT", "confidence": 0.9,
                    "reason": "password value after 'rejects password'"},
        )
        result = staleness.run_stale_check(self._client(), "m1", llm=llm)
        self.assertEqual(result["safety"], "SECRET_SUSPECT")
        self.assertTrue(result["safety_open"])
        rows = staleness.pair_store().open_pairs(kind="safety")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["old_id"], "m1")
        # Safety opens at the base 0.6 floor, unlike necessity/timestamp (0.8).
        staleness.pair_store()._conn.execute("DELETE FROM pairs")
        row = staleness.pair_store().record_judgment(
            kind=staleness.KIND_SAFETY, new_id="x", old_id="x", old_text="t",
            verdict="SECRET_SUSPECT", confidence=0.65, reason="",
        )
        self.assertEqual(row["disposition"], "open")

    def test_ledger_import_still_gets_safety_check(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "keep"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
            safety={"verdict": "SECRET_SUSPECT", "confidence": 0.9, "reason": "token value"},
        )
        result = staleness.run_stale_check(
            self._client({"origin": "ledger_import"}), "m1", llm=llm
        )
        self.assertNotIn("timestamp", result)  # ledger skips timestamp
        self.assertEqual(result["safety"], "SECRET_SUSPECT")  # but not safety
        self.assertEqual(llm.safety_calls, 1)

    def test_pinned_entry_skips_everything(self) -> None:
        llm = RoutingFakeLlm({}, {})
        result = staleness.run_stale_check(
            self._client({"stale_check_pin": True}), "m1", llm=llm
        )
        self.assertEqual(result["skipped"], "memory is pinned")
        self.assertEqual(llm.necessity_calls, 0)

    def test_ledger_import_skips_timestamp_check(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "keep"},
            {"verdict": "TIMESTAMP_SUSPECT", "confidence": 0.9, "reason": "n/a"},
        )
        result = staleness.run_stale_check(
            self._client({"origin": "ledger_import"}), "m1", llm=llm
        )
        self.assertNotIn("timestamp", result)
        self.assertEqual(llm.timestamp_calls, 0)


class TtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()

    def test_is_ttl_expired(self) -> None:
        self.assertFalse(staleness.is_ttl_expired({}))
        self.assertFalse(staleness.is_ttl_expired({"ttl_expires_at": "2999-01-01T00:00:00+00:00"}))
        self.assertTrue(staleness.is_ttl_expired({"ttl_expires_at": "2020-01-01T00:00:00+00:00"}))

    def test_set_and_clear_ttl(self) -> None:
        client = FakeClient({"m1": {"data": "snapshot"}})
        result = staleness.set_ttl(client, "m1", days=30, actor_id="claude")
        self.assertIn("ttl_expires_at", result)
        self.assertTrue(client.vector_store.payloads["m1"]["ttl_expires_at"])
        cleared = staleness.set_ttl(client, "m1", clear=True)
        self.assertTrue(cleared["ttl_cleared"])
        self.assertIsNone(client.vector_store.payloads["m1"]["ttl_expires_at"])
        with self.assertRaises(StalenessError):
            staleness.set_ttl(client, "ghost", days=1)

    def test_renewal_clears_materialized_expiry(self) -> None:
        client = FakeClient({"m1": {"data": "snapshot", "ttl_expires_at": "2020-01-01", "ttl_expired_at": "2020-01-01"}})
        staleness.set_ttl(client, "m1", days=7)
        self.assertIsNone(client.vector_store.payloads["m1"]["ttl_expired_at"])
        self.assertGreater(client.vector_store.payloads["m1"]["ttl_expires_at"], "2026")

    def test_expire_now_materializes_and_harvest_skips(self) -> None:
        client = FakeClient({"m1": {"data": "snapshot"}})
        result = staleness.set_ttl(client, "m1", expire_now=True, actor_id="claude")
        payload = client.vector_store.payloads["m1"]
        self.assertTrue(payload["ttl_expired_at"])
        self.assertEqual(payload["ttl_expires_at"], payload["ttl_expired_at"])
        self.assertIn("ttl_expired_at", result)
        # Harvest skips already-materialized expiries: no review flag opens.
        client.get_all = lambda **kw: {"results": [
            {"id": "m1", "memory": "snapshot", "metadata": dict(payload)},
        ]}
        harvest = staleness.harvest_expired(client)
        self.assertEqual(harvest["harvested"], 0)
        self.assertEqual(staleness.pair_store().open_pairs(kind="ttl_expiry"), [])

    def test_renewal_closes_open_expiry_flag(self) -> None:
        client = FakeClient({"m1": {"data": "snapshot", "ttl_expires_at": "2020-01-01", "ttl_expired_at": "2020-01-01"}})
        store = staleness.pair_store()
        row = store.record_judgment(
            kind="ttl_expiry", new_id="m1", old_id="m1", old_text="snapshot@@2020-01-01",
            verdict="TTL_EXPIRED", confidence=1.0, reason="expired",
        )
        self.assertEqual(store.get(row["pair_id"])["disposition"], "open")
        # Direct renewal (not via `stale ttl`) must also close the flag.
        staleness.set_ttl(client, "m1", days=7)
        self.assertEqual(store.get(row["pair_id"])["disposition"], "ttl")
        self.assertIsNone(client.vector_store.payloads["m1"]["ttl_expired_at"])

    def test_search_filters_expired_entries(self) -> None:
        items = [
            {"id": "live", "memory": "a", "metadata": {}},
            {"id": "gone", "memory": "b", "metadata": {"ttl_expires_at": "2020-01-01T00:00:00+00:00"}},
            {"id": "later", "memory": "c", "metadata": {"ttl_expires_at": "2999-01-01T00:00:00+00:00"}},
        ]
        client = FakeClient({}, search_items=items)
        result = search_with_staleness(
            client, query="q", top_k=3, filters=None, threshold=0.1,
            rerank=False, keyword=False, explain=False,
        )
        ids = [i["id"] for i in result["results"]]
        self.assertEqual(ids, ["live", "later"])

    def test_harvest_materializes_expiry_and_closes_pairs(self) -> None:
        client = FakeClient({"gone": {"data": "b"}})
        client.get_all = lambda **kw: {
            "results": [
                {"id": "gone", "memory": "b", "metadata": {"ttl_expires_at": "2020-01-01T00:00:00+00:00"}},
            ]
        }
        store = staleness.pair_store()
        store.record_judgment(
            kind="necessity", new_id="gone", old_id="gone", old_text="b",
            verdict="PROGRESS_TICK", confidence=0.9, reason="tick",
        )
        result = staleness.harvest_expired(client)
        self.assertEqual(result["harvested"], 1)
        self.assertTrue(client.vector_store.payloads["gone"]["ttl_expired_at"])
        # The prior necessity pair is closed; a fresh ttl_expiry flag opens
        # so review can accept the expiry or renew the TTL.
        flags = store.open_pairs(kind="ttl_expiry")
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["verdict"], "TTL_EXPIRED")
        self.assertEqual(store.open_pairs(kind="necessity"), [])

    def test_delete_guard_detects_participation(self) -> None:
        client = FakeClient({"node": {"data": "x", "superseded_by": ["y"]}})
        client.get_all = lambda **kw: {"results": []}
        self.assertTrue(staleness.delete_guard(client, "node")["participates"])

        client2 = FakeClient({"parent": {"data": "x"}})
        client2.get_all = lambda **kw: {"results": [{"id": "child"}]}
        self.assertTrue(staleness.delete_guard(client2, "parent")["participates"])

        client3 = FakeClient({"free": {"data": "x"}})
        client3.get_all = lambda **kw: {"results": []}
        self.assertFalse(staleness.delete_guard(client3, "free")["participates"])


class PairStoreMigrationTests(unittest.TestCase):
    def test_v1_table_rebuilt_with_kind(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "stale.db"
            conn = sqlite3.connect(str(db))
            conn.executescript(
                """
                CREATE TABLE pairs (
                    pair_id TEXT PRIMARY KEY, new_id TEXT NOT NULL,
                    old_id TEXT NOT NULL, old_text_hash TEXT NOT NULL,
                    verdict TEXT NOT NULL, confidence REAL, reason TEXT,
                    judged_at TEXT NOT NULL, judge_model TEXT,
                    new_session_id TEXT,
                    disposition TEXT NOT NULL DEFAULT 'open',
                    disposed_by TEXT, disposed_at TEXT,
                    UNIQUE(new_id, old_id, old_text_hash)
                );
                INSERT INTO pairs (pair_id, new_id, old_id, old_text_hash,
                    verdict, judged_at)
                VALUES ('p1', 'n', 'o', 'h', 'SUPERSEDED', '2026-07-01');
                """
            )
            conn.commit()
            conn.close()

            store = PairStore(db)
            row = store.get("p1")
            self.assertEqual(row["kind"], "displacement")
            # Same (new_id, old_id, hash) under a different kind now inserts.
            rec = store.record_judgment(
                kind="necessity", new_id="n", old_id="n", old_text="t",
                verdict="DURABLE", confidence=0.9, reason="",
            )
            self.assertTrue(rec["inserted"])

    def test_ttl_disposition_and_updated_text_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PairStore(Path(tmp) / "stale.db")
            row = store.record_judgment(
                kind="necessity", new_id="m", old_id="m", old_text="v1",
                verdict="EVENT_SCOPED", confidence=0.9, reason="pending",
            )
            self.assertTrue(store.dispose(row["pair_id"], "ttl", disposed_by="claude"))
            row2 = store.record_judgment(
                kind="necessity", new_id="m2", old_id="m2", old_text="v1",
                verdict="PROGRESS_TICK", confidence=0.9, reason="tick",
            )
            self.assertEqual(store.close_for_updated_text("m2", "v2-rewritten"), 1)
            self.assertEqual(store.get(row2["pair_id"])["disposition"], "expired")


if __name__ == "__main__":
    unittest.main()
