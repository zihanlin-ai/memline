from __future__ import annotations

import errno
import signal
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mem0_local import daemon


class FakePath:
    def __init__(self, path: str, *, exists: bool = True):
        self.path = path
        self.exists_value = exists
        self.unlink_calls = 0
        self.open_calls = 0

    def __str__(self):
        return self.path

    def exists(self):
        return self.exists_value

    def unlink(self):
        self.unlink_calls += 1
        self.exists_value = False

    def open(self, *_args, **_kwargs):
        self.open_calls += 1
        return Mock()


class FakeSocket:
    def __init__(self, *, connect_error: OSError | None = None, send_result: bool = True):
        self.connect_error = connect_error
        self.send_result = send_result
        self.timeout_values: list[float] = []
        self.connected_to: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, value):
        self.timeout_values.append(value)

    def connect(self, path):
        if self.connect_error:
            raise self.connect_error
        self.connected_to = path


class DaemonRequestTests(unittest.TestCase):
    def test_request_uses_short_connect_timeout_before_operation_timeout(self):
        fake_socket = FakeSocket()
        fake_path = FakePath("/tmp/mem0-local.sock", exists=True)
        with (
            patch.object(daemon, "SOCKET_PATH", fake_path),
            patch.object(daemon.socket, "socket", return_value=fake_socket),
            patch.object(daemon, "write_json_line", return_value=True),
            patch.object(daemon, "read_json_line", return_value={"status": "ok", "result": {"pong": True}}),
        ):
            result = daemon.request({"op": "ping"}, timeout=123, connect_timeout=4)

        self.assertEqual(result, {"pong": True})
        self.assertEqual(fake_socket.timeout_values, [4, 123])
        self.assertEqual(fake_socket.connected_to, str(fake_path))

    def test_request_reports_failed_write_as_daemon_unavailable(self):
        with (
            patch.object(daemon, "SOCKET_PATH", FakePath("/tmp/mem0-local.sock", exists=True)),
            patch.object(daemon.socket, "socket", return_value=FakeSocket()),
            patch.object(daemon, "write_json_line", return_value=False),
        ):
            with self.assertRaises(daemon.DaemonUnavailable):
                daemon.request({"op": "ping"})

    def test_request_wraps_socket_errors_as_daemon_unavailable(self):
        with (
            patch.object(daemon, "SOCKET_PATH", FakePath("/tmp/mem0-local.sock", exists=True)),
            patch.object(
                daemon.socket,
                "socket",
                return_value=FakeSocket(connect_error=PermissionError(errno.EPERM, "not permitted")),
            ),
        ):
            with self.assertRaises(daemon.DaemonUnavailable):
                daemon.request({"op": "ping"})

    def test_write_json_line_returns_false_on_broken_pipe(self):
        conn = Mock()
        conn.sendall.side_effect = BrokenPipeError()

        self.assertFalse(daemon.write_json_line(conn, {"status": "ok"}))


class DaemonLifecycleTests(unittest.TestCase):
    def test_start_daemon_recovers_stale_daemon_pid_and_unlinks_runtime_files(self):
        popen = SimpleNamespace(pid=42, poll=Mock(side_effect=[None, None]))
        with (
            patch.object(daemon, "SOCKET_PATH", FakePath("/tmp/mem0-local.sock", exists=False)),
            patch.object(daemon, "LOG_PATH", FakePath("/tmp/mem0-local.log", exists=True)),
            patch.object(daemon, "ping", side_effect=[None, {"pid": 42, "socket": "sock"}]),
            patch.object(daemon, "read_pid", return_value=7),
            patch.object(daemon, "is_pid_running", return_value=True),
            patch.object(daemon, "is_daemon_pid", return_value=True),
            patch.object(daemon, "terminate_daemon_pid", return_value=True) as terminate_daemon_pid,
            patch.object(daemon, "unlink_runtime_files") as unlink_runtime_files,
            patch.object(daemon.subprocess, "Popen", return_value=popen),
            patch.object(daemon.time, "sleep"),
        ):
            result = daemon.start_daemon(wait_seconds=1)

        self.assertEqual(result["started"], True)
        terminate_daemon_pid.assert_called_once_with(7, wait_seconds=5.0, force=True)
        unlink_runtime_files.assert_called_once()

    def test_start_daemon_cleans_child_when_startup_times_out(self):
        class Proc:
            pid = 99
            returncode = None

            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("daemon", timeout)

            def kill(self):
                self.killed = True

        proc = Proc()
        clock = SimpleNamespace(now=0.0)

        def fake_time():
            clock.now += 0.2
            return clock.now

        with (
            patch.object(daemon, "SOCKET_PATH", FakePath("/tmp/mem0-local.sock", exists=False)),
            patch.object(daemon, "LOG_PATH", FakePath("/tmp/mem0-local.log", exists=True)),
            patch.object(daemon, "ping", return_value=None),
            patch.object(daemon, "read_pid", side_effect=[None, 99]),
            patch.object(daemon.subprocess, "Popen", return_value=proc),
            patch.object(daemon.time, "time", side_effect=fake_time),
            patch.object(daemon.time, "sleep"),
            patch.object(daemon, "unlink_runtime_files") as unlink_runtime_files,
        ):
            with self.assertRaises(TimeoutError):
                daemon.start_daemon(wait_seconds=0.5)

        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        unlink_runtime_files.assert_called_once()

    def test_stop_daemon_does_not_signal_non_daemon_pid(self):
        with (
            patch.object(daemon, "read_pid", return_value=123),
            patch.object(daemon, "is_pid_running", return_value=True),
            patch.object(daemon, "is_daemon_pid", return_value=False),
            patch.object(daemon, "unlink_runtime_files") as unlink_runtime_files,
            patch.object(daemon.os, "kill") as os_kill,
        ):
            result = daemon.stop_daemon()

        self.assertEqual(result["reason"], "pid file did not point to mem0-local daemon")
        unlink_runtime_files.assert_called_once()
        os_kill.assert_not_called()

    def test_terminate_daemon_pid_uses_sigkill_when_requested(self):
        states = [True, True, True, False]

        def fake_is_running(_pid):
            return states.pop(0)

        with (
            patch.object(daemon, "is_pid_running", side_effect=fake_is_running),
            patch.object(daemon, "is_daemon_pid", return_value=True),
            patch.object(daemon.os, "kill") as os_kill,
            patch.object(daemon.time, "time", side_effect=[0, 10, 10, 11, 12, 13]),
            patch.object(daemon.time, "sleep"),
        ):
            self.assertTrue(daemon.terminate_daemon_pid(321, wait_seconds=1, force=True))

        os_kill.assert_any_call(321, signal.SIGTERM)
        os_kill.assert_any_call(321, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
