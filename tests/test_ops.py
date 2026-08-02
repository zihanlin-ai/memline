from __future__ import annotations

import unittest
from unittest.mock import patch

from memline import ops


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, memory_id):
        self.calls.append(("get", memory_id))
        return {"id": memory_id}

    def get_all(self, filters=None, top_k=100):
        self.calls.append(("get_all", filters, top_k))
        return {"results": [{"id": str(i)} for i in range(top_k)]}


class DispatchTests(unittest.TestCase):
    def test_get_routes_to_client(self):
        client = FakeClient()
        self.assertEqual(ops.dispatch(client, "get", {"memory_id": "m1"}), {"id": "m1"})
        self.assertEqual(client.calls, [("get", "m1")])

    def test_list_slices_normalized_items(self):
        client = FakeClient()
        result = ops.dispatch(
            client, "list", {"filters": None, "top_k": 10, "start": 2, "end": 5}
        )
        self.assertEqual([r["id"] for r in result], ["2", "3", "4"])

    def test_unknown_op_raises(self):
        with self.assertRaises(ValueError):
            ops.dispatch(FakeClient(), "nope", {})

    def test_protection_handler_owns_the_thirty_day_default(self):
        with patch("memline.staleness.set_displacement_protection") as setter:
            ops._set_displacement_protection(
                FakeClient(),
                {
                    "memory_id": "m1",
                    "reason": "three consecutive false positives",
                    "actor_id": "codex",
                    "session_id": "s1",
                    "force": True,
                },
            )
        self.assertEqual(setter.call_args.kwargs["days"], 30)
        self.assertNotIn("force", setter.call_args.kwargs)

class RegistryMetadataTests(unittest.TestCase):
    def test_timeouts_match_documented_defaults(self):
        self.assertEqual(ops.op_timeout("search", {"rerank": False}), 30.0)
        self.assertEqual(ops.op_timeout("search", {"rerank": True}), 180.0)
        self.assertEqual(ops.op_timeout("add", {"infer": False}), 30.0)
        self.assertEqual(ops.op_timeout("add", {"infer": True}), 300.0)
        self.assertEqual(ops.op_timeout("get", {}), 30.0)
        self.assertEqual(ops.op_timeout("event_retry", {}), 30.0)
        # Ops without an explicit entry fall back to the generous default.
        self.assertEqual(ops.op_timeout("update", {}), ops.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(ops.op_timeout("unknown", {}), ops.DEFAULT_TIMEOUT_SECONDS)

    def test_timeout_env_override_wins(self):
        with patch.dict(ops.os.environ, {"MEMLINE_DAEMON_TIMEOUT": "7.5"}, clear=False):
            self.assertEqual(ops.op_timeout("search", {"rerank": True}), 7.5)
        with patch.dict(ops.os.environ, {"MEMLINE_DAEMON_TIMEOUT": "bad"}, clear=False):
            self.assertEqual(ops.op_timeout("search", {"rerank": False}), 30.0)

    def test_set_ttl_registered(self):
        self.assertIn("set_ttl", ops.OPS)
        self.assertEqual(ops.op_timeout("set_ttl", {}), 30.0)

    def test_displacement_protection_ops_registered(self):
        for name in (
            "set_displacement_protection",
            "clear_displacement_protection",
            "list_displacement_protections",
        ):
            self.assertIn(name, ops.OPS)
            self.assertEqual(ops.op_timeout(name, {}), 30.0)

    def test_llm_slot_and_exclusive_flags(self):
        self.assertTrue(ops.is_llm_bound("add", {"infer": True}))
        self.assertTrue(ops.is_llm_bound("add", {}))  # add defaults to infer=True
        self.assertFalse(ops.is_llm_bound("add", {"infer": False}))
        self.assertTrue(ops.is_llm_bound("search", {"rerank": True}))
        self.assertFalse(ops.is_llm_bound("search", {"rerank": False}))
        self.assertTrue(ops.is_exclusive("delete", {"all": True}))
        self.assertFalse(ops.is_exclusive("delete", {"all": False}))
        self.assertFalse(ops.is_exclusive("get", {}))

    def test_every_op_has_a_handler_and_resolvable_timeout(self):
        for op, spec in ops.OPS.items():
            self.assertTrue(callable(spec.handler), op)
            timeout = spec.timeout({}) if callable(spec.timeout) else spec.timeout
            self.assertGreater(timeout, 0, op)


if __name__ == "__main__":
    unittest.main()
