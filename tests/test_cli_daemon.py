from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click

from mem0_local import cli
from mem0_local.daemon import DaemonUnavailable


class FakePath:
    def __init__(self, exists: bool):
        self.exists_value = exists

    def exists(self):
        return self.exists_value


class CliDaemonTests(unittest.TestCase):
    def test_maybe_daemon_request_falls_back_when_no_runtime_files_exist(self):
        with (
            patch.dict(cli.os.environ, {}, clear=False),
            patch("mem0_local.daemon.SOCKET_PATH", FakePath(False)),
            patch("mem0_local.daemon.PID_PATH", FakePath(False)),
            patch("mem0_local.daemon.request", side_effect=DaemonUnavailable("missing socket")),
        ):
            used, result = cli.maybe_daemon_request("search", {"rerank": False})

        self.assertFalse(used)
        self.assertIsNone(result)

    def test_maybe_daemon_request_fails_fast_when_runtime_files_exist_but_daemon_unreachable(self):
        with (
            patch.dict(cli.os.environ, {}, clear=False),
            patch("mem0_local.daemon.SOCKET_PATH", FakePath(True)),
            patch("mem0_local.daemon.PID_PATH", FakePath(True)),
            patch("mem0_local.daemon.request", side_effect=DaemonUnavailable("permission denied")),
        ):
            with self.assertRaises(click.ClickException) as raised:
                cli.maybe_daemon_request("search", {"rerank": False})

        self.assertIn("daemon appears to be configured but is not reachable", str(raised.exception))

    def test_daemon_timeout_defaults_are_short_for_base_search(self):
        self.assertEqual(cli.daemon_operation_timeout("search", {"rerank": False}), 30.0)
        self.assertEqual(cli.daemon_operation_timeout("search", {"rerank": True}), 180.0)
        self.assertEqual(cli.daemon_operation_timeout("add", {"infer": False}), 30.0)
        self.assertEqual(cli.daemon_operation_timeout("add", {"infer": True}), 300.0)

    def test_daemon_timeout_can_be_overridden(self):
        with patch.dict(cli.os.environ, {"MEM0_LOCAL_DAEMON_TIMEOUT": "7.5"}, clear=False):
            self.assertEqual(cli.daemon_operation_timeout("search", {"rerank": True}), 7.5)

    def test_invalid_timeout_override_uses_default(self):
        with patch.dict(cli.os.environ, {"MEM0_LOCAL_DAEMON_TIMEOUT": "bad"}, clear=False):
            self.assertEqual(cli.daemon_operation_timeout("search", {"rerank": False}), 30.0)

    def test_add_appends_live_audit_after_successful_daemon_add(self):
        result = {"results": [{"id": "memory-1", "memory": "Keep audit manifests.", "event": "ADD"}]}
        from mem0_local import queue as queue_mod

        with (
            patch.dict(cli.os.environ, {}, clear=False),
            patch.object(cli, "maybe_daemon_request", return_value=(True, result)),
            patch("mem0_local.audit.append_live_audit") as append_live_audit,
            patch.object(cli, "output") as output,
            patch.object(queue_mod, "EventQueue") as event_queue,
        ):
            event_queue.return_value.enqueue.return_value = "test-event"
            cli.add(
                text="Keep audit manifests.",
                user_id="workspace",
                agent_id=None,
                app_id=None,
                run_id=None,
                messages=None,
                file=None,
                metadata=[],
                timestamp=None,
                ledger_timestamp=None,
                infer_opt=None,
                supersedes=None,
                wait=False,
                json_flag=True,
                output_format="json",
            )

        append_live_audit.assert_called_once()
        kwargs = append_live_audit.call_args.kwargs
        self.assertEqual(kwargs["operation"], "add")
        self.assertEqual(kwargs["input_payload"]["content"], "Keep audit manifests.")
        # Plain-text adds store raw verbatim by default since 2026-07-16;
        # extraction requires --infer or --messages/--file input.
        self.assertFalse(kwargs["input_payload"]["infer"])
        self.assertEqual(kwargs["result"]["results"][0]["id"], "memory-1")
        output.assert_called_once()

    def test_stale_protect_routes_only_policy_inputs_to_core_setter(self):
        self.assertNotIn("force", inspect.signature(cli.stale_protect).parameters)
        span = SimpleNamespace(result=None)
        result = {
            "id": "memory-1",
            "kind": "displacement",
            "protected_until": "2026-08-21T00:00:00+00:00",
            "reason": "user approved",
            "dismissed_evidence_count": 3,
        }
        with (
            patch.object(cli, "audited") as audited,
            patch.object(cli, "execute", return_value=result) as execute,
            patch.object(cli, "output") as output,
        ):
            audited.return_value.__enter__.return_value = span
            cli.stale_protect(
                memory_id="memory-1",
                kind="displacement",
                days=30,
                reason="user approved",
                json_flag=True,
                output_format="json",
            )

        op_args = execute.call_args.args[1]
        self.assertEqual(execute.call_args.args[0], "set_displacement_protection")
        self.assertNotIn("force", op_args)
        self.assertNotIn("dismissed_evidence_count", op_args)
        self.assertTrue(op_args["actor_id"])
        self.assertTrue(op_args["session_id"])
        self.assertEqual(span.result, result)
        output.assert_called_once()

    def test_stale_dismiss_obeys_disposition_authority(self):
        store = MagicMock()
        pair = {
            "id": "pair-1",
            "kind": "displacement",
            "disposition": "open",
            "new_session_id": "other-session",
        }
        with (
            patch.object(cli, "_load_open_pair", return_value=(store, pair)),
            patch.object(cli, "_interactive_tty", return_value=False),
            patch.object(
                cli,
                "detect_writer_context",
                return_value={"session_id": "current-session", "source": "codex"},
            ),
        ):
            with self.assertRaises(click.ClickException) as raised:
                cli.stale_dismiss(
                    pair_id="pair-1",
                    force=False,
                    json_flag=True,
                    output_format="json",
                )
        self.assertIn("dismiss denied", str(raised.exception))
        store.dispose.assert_not_called()

        with (
            patch.object(cli, "_load_open_pair", return_value=(store, pair)),
            patch.object(cli, "_interactive_tty", return_value=False),
            patch.object(
                cli,
                "detect_writer_context",
                return_value={"session_id": "current-session", "source": "codex"},
            ),
            patch.object(cli, "output"),
        ):
            cli.stale_dismiss(
                pair_id="pair-1",
                force=True,
                json_flag=True,
                output_format="json",
            )
        store.dispose.assert_called_once_with(
            "pair-1", "dismissed", disposed_by="codex"
        )

    def test_cross_session_disposition_requires_explicit_tty_confirmation(self):
        pair = {
            "pair_id": "pair-1",
            "kind": "displacement",
            "new_session_id": "other-session",
        }
        with (
            patch.object(cli, "detect_writer_context", return_value={"session_id": "mine"}),
            patch.object(cli, "_interactive_tty", return_value=True),
            patch.object(cli.click, "confirm", return_value=True) as confirm,
        ):
            cli._require_disposition_authority(pair, False, "dismiss")
        confirm.assert_called_once_with(
            "Pair pair-1 belongs to another session. Confirm dismiss?",
            default=False,
        )

        with (
            patch.object(cli, "detect_writer_context", return_value={"session_id": "mine"}),
            patch.object(cli, "_interactive_tty", return_value=True),
            patch.object(cli.click, "confirm", return_value=False),
        ):
            with self.assertRaises(click.ClickException) as raised:
                cli._require_disposition_authority(pair, False, "dismiss")
        self.assertIn("confirmation declined", str(raised.exception))

if __name__ == "__main__":
    unittest.main()
