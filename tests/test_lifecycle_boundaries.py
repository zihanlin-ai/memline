"""Scenario boundary tests for the memory lifecycle (design fence).

Unlike the per-module unit tests, every test here encodes a WORKFLOW
contract from the lifecycle design (reference/mem-lifecycle-design doc):
the session-handoff review ritual, the multi-cycle TTL loop, disposition
authority and finality, supersession-chain integrity under delete/expiry,
judge self-check boundaries, and the four standing invariants:

  I1 marks never execute   — judging alone must not change store state
  I2 loss only at audit    — every exit from the pool is reversible
  I3 lazy correctness      — pool membership never depends on the daemon
  I4 decisions are final   — a reviewed call is never re-asked by harvest
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from test_staleness import FakeClient, RoutingFakeLlm

from memline import staleness
from memline.ops import dispatch as dispatch_op
from memline.staleness import (
    KIND_NECESSITY,
    KIND_TTL_EXPIRY,
    PairStore,
    StalenessError,
    invalidate,
    resolve_head,
    revive,
    search_with_staleness,
    set_ttl,
    harvest_expired,
)


class _EmptyQueue:
    """Stands in for the event queue when a test only cares about the verdict:
    no pending stale checks, no failed adds."""

    def list(self, status: str | None = None, limit: int = 500) -> list:
        return []

    def get_args(self, event_id: str) -> dict:
        return {}


class LifecycleFakeClient(FakeClient):
    """FakeClient + the surface the lifecycle ops touch (get_all/update/delete)."""

    def get_all(self, filters=None, top_k=100):
        filters = filters or {}
        rows = []
        for mid, payload in self.vector_store.payloads.items():
            if "superseded_by" in filters:
                wanted = filters["superseded_by"]
                if wanted not in (payload.get("superseded_by") or []):
                    continue
            if "ttl_expires_at" in filters:
                bound = filters["ttl_expires_at"]
                expires = payload.get("ttl_expires_at")
                if not expires or (isinstance(bound, dict) and "lte" in bound and str(expires) > bound["lte"]):
                    continue
            rows.append(
                {
                    "id": mid,
                    "memory": payload.get("data", ""),
                    "metadata": {k: v for k, v in payload.items() if k != "data"},
                }
            )
        return {"results": rows[:top_k]}

    def update(self, memory_id, text, metadata=None):
        self.vector_store.payloads[memory_id]["data"] = text
        return {"id": memory_id, "updated": True}

    def delete(self, memory_id):
        del self.vector_store.payloads[memory_id]
        return {"message": "deleted"}


def payload_getter(client):
    def _get(mid):
        point = client.vector_store.get(mid)
        return dict(point.payload) if point is not None else None

    return _get


class ScratchPairStoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        staleness._pair_store = PairStore(Path(self._tmp.name) / "stale.db")
        self.store = staleness.pair_store()

    def tearDown(self) -> None:
        staleness._pair_store = None
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# A. Multi-cycle TTL loop
# ---------------------------------------------------------------------------


class TtlLoopBoundaryTests(ScratchPairStoreCase):
    def _expired_client(self):
        return LifecycleFakeClient(
            {"m1": {"data": "snapshot", "ttl_expires_at": "2020-01-01T00:00:00+00:00"}}
        )

    def test_two_expiry_cycles_rearm_exactly_one_flag_each(self) -> None:
        """expire -> flag -> renew -> expire again -> a NEW flag (deadline salt)."""
        client = self._expired_client()
        harvest_expired(client)
        first = self.store.open_pairs(kind=KIND_TTL_EXPIRY)
        self.assertEqual(len(first), 1)

        # Reviewer renews; flag closes; entry re-enters the pool.
        self.store.dispose(first[0]["pair_id"], "ttl", disposed_by="user")
        set_ttl(client, "m1", days=7)
        self.assertEqual(self.store.open_pairs(kind=KIND_TTL_EXPIRY), [])

        # Second cycle: force the new deadline into the past -> a NEW flag.
        client.vector_store.payloads["m1"]["ttl_expires_at"] = "2020-02-02T00:00:00+00:00"
        harvest_expired(client)
        second = self.store.open_pairs(kind=KIND_TTL_EXPIRY)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0]["pair_id"], first[0]["pair_id"])

    def test_harvest_is_idempotent_within_one_cycle(self) -> None:
        client = self._expired_client()
        self.assertEqual(harvest_expired(client)["harvested"], 1)
        self.assertEqual(harvest_expired(client)["harvested"], 0)
        self.assertEqual(len(self.store.open_pairs(kind=KIND_TTL_EXPIRY)), 1)

    def test_disposed_flag_does_not_reopen_within_same_deadline(self) -> None:
        """Accepting an expiry is final for that deadline (I4): even if the
        materialized stamp were lost, the same deadline never re-flags."""
        client = self._expired_client()
        harvest_expired(client)
        flag = self.store.open_pairs(kind=KIND_TTL_EXPIRY)[0]
        self.store.dispose(flag["pair_id"], "confirmed", disposed_by="user")
        # Simulate a lost stamp (worst case): same deadline -> no new flag.
        client.vector_store.payloads["m1"].pop("ttl_expired_at", None)
        harvest_expired(client)
        self.assertEqual(self.store.open_pairs(kind=KIND_TTL_EXPIRY), [])

    def test_lazy_filter_needs_no_harvest_and_raw_view_keeps_everything(self) -> None:
        """I3 + I2: an expired entry is out of the pool before any harvest
        runs, and --include-superseded still surfaces it."""
        items = [
            {"id": "gone", "memory": "x", "metadata": {"ttl_expires_at": "2020-01-01T00:00:00+00:00"}},
            {"id": "live", "memory": "y", "metadata": {}},
        ]
        client = FakeClient({}, search_items=items)
        kwargs = dict(query="q", top_k=5, filters=None, threshold=0.1,
                      rerank=False, keyword=False, explain=False)
        filtered = search_with_staleness(client, **kwargs)
        self.assertEqual([i["id"] for i in filtered["results"]], ["live"])
        raw = search_with_staleness(client, include_superseded=True, **kwargs)
        self.assertEqual([i["id"] for i in raw["results"]], ["gone", "live"])

    def test_deadline_boundary_is_inclusive(self) -> None:
        now = "2026-07-21T12:00:00+00:00"
        self.assertTrue(staleness.is_ttl_expired({"ttl_expires_at": now}, now))
        self.assertFalse(
            staleness.is_ttl_expired({"ttl_expires_at": "2026-07-21T12:00:00.000001+00:00"}, now)
        )

    def test_clear_after_expiry_restores_pool_and_closes_flag(self) -> None:
        client = self._expired_client()
        harvest_expired(client)
        set_ttl(client, "m1", clear=True)
        payload = client.vector_store.payloads["m1"]
        self.assertIsNone(payload["ttl_expires_at"])
        self.assertIsNone(payload["ttl_expired_at"])
        self.assertEqual(self.store.open_pairs(kind=KIND_TTL_EXPIRY), [])

    def test_revive_and_ttl_are_independent_exits(self) -> None:
        """A superseded AND expired entry needs BOTH reversals to re-enter:
        revive undoes supersession only, `ttl --clear` undoes expiry only."""
        client = LifecycleFakeClient({
            "old": {"data": "v1", "ttl_expires_at": "2020-01-01T00:00:00+00:00"},
            "new": {"data": "v2"},
        })
        invalidate(client, "old", ["new"])
        revive(client, "old")
        # Supersession is gone but the TTL exit still stands.
        self.assertEqual(staleness.superseded_ids(client.vector_store.payloads["old"]), [])
        self.assertTrue(staleness.is_ttl_expired(client.vector_store.payloads["old"]))
        set_ttl(client, "old", clear=True)
        self.assertFalse(staleness.is_ttl_expired(client.vector_store.payloads["old"]))


# ---------------------------------------------------------------------------
# B. Disposition finality and kind guards
# ---------------------------------------------------------------------------


class DispositionBoundaryTests(ScratchPairStoreCase):
    def test_reviewed_expiry_is_never_re_asked(self) -> None:
        """I4 end-to-end: necessity confirm materializes instantly, so the
        harvest loop finds nothing to flag."""
        client = LifecycleFakeClient({"m1": {"data": "tick"}})
        set_ttl(client, "m1", expire_now=True, actor_id="user")
        self.assertEqual(harvest_expired(client)["harvested"], 0)
        self.assertEqual(self.store.open_pairs(kind=KIND_TTL_EXPIRY), [])

    def test_delete_downgrade_is_a_final_decision(self) -> None:
        """Chain participant delete -> downgraded to expiry; the downgrade is
        human-initiated, so harvest must not re-open it either."""
        client = LifecycleFakeClient({
            "old": {"data": "v1"},
            "new": {"data": "v2"},
        })
        invalidate(client, "old", ["new"])
        result = dispatch_op(client, "delete", {"all": False, "memory_id": "old"})
        self.assertTrue(result["downgraded_to_expiry"])
        self.assertIn("old", client.vector_store.payloads)  # never hard-deleted
        self.assertEqual(harvest_expired(client)["harvested"], 0)
        self.assertEqual(self.store.open_pairs(kind=KIND_TTL_EXPIRY), [])

    def test_dispose_semantics_are_single_shot(self) -> None:
        row = self.store.record_judgment(
            kind=KIND_NECESSITY, new_id="m", old_id="m", old_text="t",
            verdict="EXPIRING", confidence=0.9, reason="tick",
        )
        self.assertTrue(self.store.dispose(row["pair_id"], "ttl"))
        self.assertFalse(self.store.dispose(row["pair_id"], "confirmed"))
        # ttl is one of the sanctioned mutation-follow-up rollback states.
        self.assertTrue(self.store.reopen(row["pair_id"]))
        self.assertFalse(self.store.reopen(row["pair_id"]))

    def test_confidence_floors_are_kind_specific_and_exact(self) -> None:
        cases = [
            (staleness.KIND_DISPLACEMENT, "SUPERSEDED", 0.60, "open"),
            (staleness.KIND_DISPLACEMENT, "SUPERSEDED", 0.59, "cached"),
            (KIND_NECESSITY, "EXPIRING", 0.80, "open"),
            (KIND_NECESSITY, "EXPIRING", 0.79, "cached"),
            (staleness.KIND_CORRECTNESS, "TIMESTAMP_SUSPECT", 0.80, "open"),
            (staleness.KIND_CORRECTNESS, "TIMESTAMP_SUSPECT", 0.79, "cached"),
        ]
        for i, (kind, verdict, conf, expected) in enumerate(cases):
            row = self.store.record_judgment(
                kind=kind, new_id=f"n{i}", old_id=f"o{i}", old_text="t",
                verdict=verdict, confidence=conf, reason="",
            )
            self.assertEqual(row["disposition"], expected, msg=f"{kind}@{conf}")

    def test_every_live_verdict_routes_to_its_disposition_family(self) -> None:
        """Guidance is keyed on the judge's current vocabulary. The pre-v5
        5-way necessity verdicts are NOT handled here: commit 8c48bda remapped
        all 570 stored marks by SQL in the same change that introduced v5, and
        parse_single_judgment coerces any out-of-vocabulary verdict to DURABLE,
        so no row can carry one from either direction."""
        from memline.review import flag_suggestion as _flag_suggestion

        play = _flag_suggestion({"kind": "necessity", "verdict": "EXPIRING"})
        self.assertEqual(play["verdict"], "EXPIRING")
        self.assertIn("stale ttl", play["fix"])
        play = _flag_suggestion({"kind": "necessity", "verdict": "BORN_UNNECESSARY"})
        self.assertIn("stale confirm", play["fix"])
        self.assertIn("renews", _flag_suggestion({"kind": "ttl_expiry", "verdict": "TTL_EXPIRED"})["partial"])
        self.assertIn("update", _flag_suggestion({"kind": "correctness", "verdict": "TIMESTAMP_SUSPECT"})["fix"])

    def test_an_unknown_verdict_still_gets_usable_guidance(self) -> None:
        """The fallback is the only thing standing behind a future verdict
        added to a judge before this table learns about it."""
        from memline.review import flag_suggestion as _flag_suggestion

        play = _flag_suggestion({"kind": "necessity", "verdict": "SOMETHING_NEW"})
        self.assertIn("SOMETHING_NEW", play["means"])
        self.assertTrue(play["fix"])
        self.assertTrue(play["dismiss_only_if"])

    def test_every_flag_carries_its_verdict_playbook(self) -> None:
        """Review must state what a finding means and the one condition under
        which dismissing it is right -- dismissal is permanent per text version,
        so 'acknowledge and move on' has to be visibly the wrong door."""
        from memline.review import flag_suggestion as _flag_suggestion

        for kind, verdict in (
            ("correctness", "LANGUAGE_SUSPECT"),
            ("correctness", "ATTRIBUTION_SUSPECT"),
            ("safety", "SECRET_SUSPECT"),
            ("necessity", "BORN_UNNECESSARY"),
            ("displacement", "SUPERSEDED"),
            ("displacement", "DUPLICATE"),
            ("displacement", "SOME_FUTURE_VERDICT"),
        ):
            play = _flag_suggestion({"kind": kind, "verdict": verdict})
            for field in ("means", "fix", "dismiss_only_if", "dismiss", "warning"):
                self.assertTrue(play.get(field), f"{kind}/{verdict} missing {field}")
            self.assertIn("PERMANENT", play["warning"])

        lang = _flag_suggestion({"kind": "correctness", "verdict": "LANGUAGE_SUSPECT"})
        self.assertIn("English", lang["fix"])
        # The audit failure mode: entries dismissed because their facts were
        # right, not because their narration was already English.
        self.assertIn("not merely because the facts are correct", lang["dismiss_only_if"])


# ---------------------------------------------------------------------------
# C. Supersession-chain integrity under delete/expiry
# ---------------------------------------------------------------------------


class ChainIntegrityTests(ScratchPairStoreCase):
    def test_no_delete_path_may_dangle_a_chain_pointer(self) -> None:
        """Both sides of an edge are protected: the superseded node and the
        superseder both downgrade instead of vanishing."""
        client = LifecycleFakeClient({"old": {"data": "v1"}, "new": {"data": "v2"}})
        invalidate(client, "old", ["new"])
        for node in ("old", "new"):
            result = dispatch_op(client, "delete", {"all": False, "memory_id": node})
            self.assertTrue(result.get("downgraded_to_expiry"), msg=node)
        # The chain still resolves after both "deletions".
        heads = resolve_head(payload_getter(client), "old")
        self.assertEqual(heads["heads"], ["new"])

    def test_resolve_head_walks_through_expired_nodes(self) -> None:
        """TTL expiry is pool-membership, not chain surgery: an expired head
        is still the head."""
        client = LifecycleFakeClient({"old": {"data": "v1"}, "new": {"data": "v2"}})
        invalidate(client, "old", ["new"])
        set_ttl(client, "new", expire_now=True)
        self.assertEqual(resolve_head(payload_getter(client), "old")["heads"], ["new"])

    def test_free_node_delete_closes_its_suspicions(self) -> None:
        client = LifecycleFakeClient({"free": {"data": "x"}})
        self.store.record_judgment(
            kind=KIND_NECESSITY, new_id="free", old_id="free", old_text="x",
            verdict="BORN_UNNECESSARY", confidence=0.9, reason="",
        )
        result = dispatch_op(client, "delete", {"all": False, "memory_id": "free"})
        self.assertNotIn("downgraded_to_expiry", result if isinstance(result, dict) else {})
        self.store.close_for_deleted_memory("free")  # CLI layer contract
        self.assertEqual(self.store.open_count(), 0)


# ---------------------------------------------------------------------------
# D. Judge self-check boundaries
# ---------------------------------------------------------------------------


class SelfCheckBoundaryTests(ScratchPairStoreCase):
    def _client(self, payload_extra=None):
        payload = {
            "data": "grid progress: 42%",
            "user_id": "workspace",
            "created_at": "2026-07-21T01:00:00+00:00",
            "ingested_at": "2026-07-21T01:00:00+00:00",
            "source": "claude",
        }
        payload.update(payload_extra or {})
        return LifecycleFakeClient({"m1": payload})

    def test_text_update_rearms_self_checks_and_expires_old_flags(self) -> None:
        """update = a new text version: old open flags die, judges re-run."""
        llm = RoutingFakeLlm(
            {"verdict": "EXPIRING", "confidence": 0.9, "reason": "tick"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
        )
        client = self._client()
        staleness.run_stale_check(client, "m1", llm=llm)
        self.assertEqual(len(self.store.open_pairs(kind=KIND_NECESSITY)), 1)
        for new_id in ("n1", "n2", "n3"):
            row = self.store.record_judgment(
                new_id=new_id,
                old_id="m1",
                old_text="grid progress: 42%",
                verdict="SUPERSEDED",
                confidence=0.9,
                reason="false positive",
            )
            self.store.dispose(row["pair_id"], "dismissed", disposed_by="reviewer")
        staleness.set_displacement_protection(
            client,
            "m1",
            days=30,
            reason="repeated false positives",
            actor_id="codex",
            session_id="s1",
        )
        self.assertTrue(
            staleness.is_displacement_protected(client.vector_store.payloads["m1"])
        )

        update_result = dispatch_op(
            client,
            "update",
            {"memory_id": "m1", "text": "final result: loss 0.43", "metadata": {}},
        )
        self.assertEqual(self.store.open_pairs(kind=KIND_NECESSITY), [])
        self.assertTrue(update_result["displacement_protection_cleared"])
        self.assertFalse(
            staleness.is_displacement_protected(client.vector_store.payloads["m1"])
        )

        llm2 = RoutingFakeLlm(
            {"verdict": "DURABLE", "confidence": 0.9, "reason": "result"},
            {"verdict": "CONSISTENT", "confidence": 0.9, "reason": "ok"},
        )
        report = staleness.run_stale_check(client, "m1", llm=llm2)
        self.assertEqual(report["necessity"], "DURABLE")
        self.assertEqual(llm2.necessity_calls, 1)  # re-armed, not cached

    def test_marks_never_execute(self) -> None:
        """I1: a flag-everything judge changes zero store state."""
        llm = RoutingFakeLlm(
            {"verdict": "BORN_UNNECESSARY", "confidence": 0.99, "reason": "log"},
            {"verdict": "TIMESTAMP_SUSPECT", "confidence": 0.99, "reason": "off"},
        )
        client = self._client()
        before = dict(client.vector_store.payloads["m1"])
        staleness.run_stale_check(client, "m1", llm=llm)
        self.assertEqual(client.vector_store.payloads["m1"], before)
        self.assertEqual(client.db.rows, [])  # no history events either

    def test_low_confidence_flags_never_reach_review(self) -> None:
        llm = RoutingFakeLlm(
            {"verdict": "BORN_UNNECESSARY", "confidence": 0.79, "reason": "log"},
            {"verdict": "TIMESTAMP_SUSPECT", "confidence": 0.5, "reason": "off"},
        )
        report = staleness.run_stale_check(self._client(), "m1", llm=llm)
        self.assertFalse(report["necessity_open"])
        self.assertFalse(report["correctness_open"])
        self.assertEqual(self.store.open_count(), 0)
        # But both verdicts are version-cached: no repeat LLM spend.
        again = staleness.run_stale_check(self._client(), "m1", llm=llm)
        self.assertEqual(again["necessity"], "cached")
        self.assertEqual(again["correctness"], "cached")


# ---------------------------------------------------------------------------
# E. Review-flow contract at the CLI seam
# ---------------------------------------------------------------------------


class ReviewFlowContractTests(ScratchPairStoreCase):
    def test_confirm_failure_reopens_the_pair(self) -> None:
        """The rollback invariant at the CLI layer: if the follow-up mutation
        fails, the suspicion returns to open instead of stranding."""
        from unittest.mock import patch
        import click
        from memline import cli

        row = self.store.record_judgment(
            kind=KIND_NECESSITY, new_id="ghost", old_id="ghost", old_text="t",
            verdict="BORN_UNNECESSARY", confidence=0.9, reason="", new_session_id="s1",
        )
        with (
            patch.object(cli._support, "execute", side_effect=StalenessError("memory not found: ghost")),
            patch("memline.audit.append_live_audit"),
            patch.object(cli._support, "detect_writer_context", return_value={"session_id": "s1", "source": "claude"}),
            patch.object(cli._support, "_interactive_tty", return_value=False),
        ):
            with self.assertRaises((click.ClickException, StalenessError)):
                cli.stale_confirm(
                    pair_id=row["pair_id"], force=False, json_flag=True, output_format="json"
                )
        self.assertEqual(self.store.get(row["pair_id"])["disposition"], "open")

    def test_cross_session_confirm_is_denied_non_interactively(self) -> None:
        from unittest.mock import patch
        import click
        from memline import cli

        row = self.store.record_judgment(
            kind=KIND_NECESSITY, new_id="m", old_id="m", old_text="t",
            verdict="BORN_UNNECESSARY", confidence=0.9, reason="",
            new_session_id="someone-elses-session",
        )
        with (
            patch.object(cli._support, "detect_writer_context", return_value={"session_id": "mine"}),
            patch.object(cli._support, "_interactive_tty", return_value=False),
        ):
            with self.assertRaises(click.ClickException):
                cli.stale_confirm(
                    pair_id=row["pair_id"], force=False, json_flag=True, output_format="json"
                )
        self.assertEqual(self.store.get(row["pair_id"])["disposition"], "open")

    def test_ttl_expiry_flags_are_disposable_by_any_session(self) -> None:
        """Expiry flags are sessionless lifecycle events: a non-interactive
        session that did NOT write the entry may still accept the expiry."""
        from unittest.mock import patch
        from memline import cli

        row = self.store.record_judgment(
            kind=KIND_TTL_EXPIRY, new_id="m", old_id="m", old_text="t@@deadline",
            verdict="TTL_EXPIRED", confidence=1.0, reason="expired",
        )
        with (
            patch.object(cli._support, "detect_writer_context", return_value={"session_id": "someone", "source": "claude"}),
            patch.object(cli._support, "_interactive_tty", return_value=False),
            patch.object(cli._support, "output"),
        ):
            cli.stale_confirm(
                pair_id=row["pair_id"], force=False, json_flag=True, output_format="json"
            )
        self.assertEqual(self.store.get(row["pair_id"])["disposition"], "confirmed")

    def test_verdict_blocks_while_a_ttl_expiry_is_unreviewed(self) -> None:
        """Disposition authority for TTL expiries belongs to every session, so
        they are nobody's property -- and therefore would be nobody's
        obligation if the verdict ignored them. A session that can dispose
        them must not be able to pass while leaving them piled up."""
        from unittest.mock import patch
        from memline import cli

        row = self.store.record_judgment(
            kind=KIND_TTL_EXPIRY, new_id="m", old_id="m", old_text="t@@deadline",
            verdict="TTL_EXPIRED", confidence=1.0, reason="expired",
        )
        captured: dict = {}

        def run() -> None:
            with (
                patch.object(cli._support, "detect_writer_context", return_value={"session_id": "s", "source": "claude"}),
                patch.object(cli._support, "execute", return_value=[]),
                patch.object(cli._support, "_event_queue_direct", return_value=_EmptyQueue()),
                # memory previews flow through the patched execute now; the
                # verdict under test never depended on their content
                patch.object(cli._support, "output", side_effect=lambda payload, **kw: captured.update(payload)),
            ):
                cli.review(session=None, wait=False, check=False, json_flag=True, output_format="json")

        run()
        self.assertEqual(captured["verdict"], "blocked")
        kinds = [b["kind"] for b in captured["blocking"]]
        self.assertIn("ttl_expired", kinds)
        self.assertEqual(
            [b for b in captured["blocking"] if b["kind"] == "ttl_expired"][0]["pair_ids"],
            [row["pair_id"]],
        )

        # Disposing it is the only thing standing between this session and a pass.
        self.store.dispose(row["pair_id"], "confirmed", disposed_by="s")
        captured.clear()
        run()
        self.assertEqual(captured["verdict"], "pass")
        self.assertEqual(captured["blocking"], [])

    def test_kind_guards_reject_wrong_dispositions(self) -> None:
        from unittest.mock import patch
        import click
        from memline import cli

        ts = self.store.record_judgment(
            kind=staleness.KIND_CORRECTNESS, new_id="m", old_id="m", old_text="t",
            verdict="TIMESTAMP_SUSPECT", confidence=0.9, reason="",
        )
        disp = self.store.record_judgment(
            new_id="n", old_id="o", old_text="t",
            verdict="SUPERSEDED", confidence=0.9, reason="",
        )
        with patch.object(cli._support, "detect_writer_context", return_value={}):
            with self.assertRaises(click.ClickException):
                cli.stale_confirm(pair_id=ts["pair_id"], force=True, json_flag=True, output_format="json")
            with self.assertRaises(click.ClickException):
                cli.stale_ttl(pair_id=disp["pair_id"], days=None, json_flag=True, output_format="json")
            with self.assertRaises(click.ClickException):
                cli.stale_merge(
                    pair_id=ts["pair_id"], merged_text="x", force=True,
                    json_flag=True, output_format="json",
                )
        self.assertEqual(self.store.get(ts["pair_id"])["disposition"], "open")
        self.assertEqual(self.store.get(disp["pair_id"])["disposition"], "open")


if __name__ == "__main__":
    unittest.main()
