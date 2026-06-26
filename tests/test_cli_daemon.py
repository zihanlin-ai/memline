from __future__ import annotations

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
