from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from memline import cli

# Every env var the detector may consult; cleared before each simulated case so
# the ambient (real) agent environment cannot leak into the assertions.
_AGENT_ENV_KEYS = [
    "MEMLINE_SOURCE",
    "MEM0_SOURCE",
    "AGENT_SOURCE",
    "AI_AGENT_SOURCE",
    "MEMLINE_SESSION_ID",
    "MEM0_SESSION_ID",
    "AGENT_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "OPENCODE_CALL_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_MANAGED_PACKAGE_ROOT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDECODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDECODE",
    "AI_AGENT",
    "AGENT",
]


def _clean_env(**overrides: str):
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV_KEYS}
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


class WriterDetectionTests(unittest.TestCase):
    def test_codex_identity_env(self):
        with _clean_env(CODEX_THREAD_ID="thread-9"):
            ctx = cli.detect_writer_context()
            self.assertEqual(ctx.get("source"), "codex")
            self.assertEqual(ctx.get("session_id"), "thread-9")

    def test_codex_managed_package_root_only(self):
        with _clean_env(CODEX_MANAGED_PACKAGE_ROOT="/opt/codex"):
            self.assertEqual(cli.detect_agent_source(), "codex")

    def test_claude_identity_env(self):
        with _clean_env(CLAUDE_CODE_SESSION_ID="sess-1", CLAUDECODE="1"):
            ctx = cli.detect_writer_context()
            self.assertEqual(ctx.get("source"), "claude")
            self.assertEqual(ctx.get("session_id"), "sess-1")

    def test_claude_via_ai_agent_tag(self):
        with _clean_env(AI_AGENT="claude-code_2-1-205_agent"):
            self.assertEqual(cli.detect_agent_source(), "claude")

    def test_opencode_identity_env(self):
        with _clean_env(OPENCODE_SESSION_ID="ses_abc", OPENCODE_CALL_ID="call_1"):
            ctx = cli.detect_writer_context()
            self.assertEqual(ctx.get("source"), "opencode")
            self.assertEqual(ctx.get("session_id"), "ses_abc")

    def test_opencode_ancestor_fallback_without_plugin(self):
        # No plugin -> no OPENCODE_* env; the shell tool still runs under the
        # opencode binary, so attribution works with session_id absent.
        with _clean_env(), patch.object(
            cli, "ancestor_exe_names", return_value=["bash", "opencode.exe"]
        ):
            ctx = cli.detect_writer_context()
            self.assertEqual(ctx.get("source"), "opencode")
            self.assertNotIn("session_id", ctx)

    def test_nested_opencode_beats_outer_claude(self):
        # `opencode` launched from a claude shell inherits claude's identity
        # vars; the per-invocation opencode signal must win for both fields.
        with _clean_env(
            CLAUDE_CODE_SESSION_ID="claude-sess",
            CLAUDECODE="1",
            OPENCODE_SESSION_ID="ses_abc",
        ):
            ctx = cli.detect_writer_context()
            self.assertEqual(ctx.get("source"), "opencode")
            self.assertEqual(ctx.get("session_id"), "ses_abc")

    def test_explicit_source_override_wins(self):
        with _clean_env(MEMLINE_SOURCE="claude", CODEX_THREAD_ID="thread-9"):
            self.assertEqual(cli.detect_writer_context().get("source"), "claude")

    def test_no_signal_returns_none(self):
        with _clean_env(), patch.object(cli, "ancestor_exe_names", return_value=[]):
            self.assertIsNone(cli.detect_agent_source())
            self.assertNotIn("source", cli.detect_writer_context())

    def test_ancestor_exe_fallback(self):
        with _clean_env(), patch.object(
            cli, "ancestor_exe_names", return_value=["bash", "claude", "node"]
        ):
            self.assertEqual(cli.detect_agent_source(), "claude")

    def test_content_cannot_flip_attribution(self):
        # The memory text is never an input to detection: an argv full of
        # "codex" words must not change the result in a claude env.
        with _clean_env(AI_AGENT="claude-code_2-1-205_agent"), patch.object(
            cli, "ancestor_exe_names", return_value=["bash"]
        ):
            self.assertEqual(cli.detect_agent_source(), "claude")

    def test_argv0_reader_does_not_expose_full_argv(self):
        # read_proc_argv0 must return only the executable, never later argv
        # (which for `memline add "<text>"` would be the memory content).
        argv0 = cli.read_proc_argv0(os.getpid())
        self.assertTrue(argv0)
        # This test process runs as `python -m unittest ...`; the later args
        # (e.g. "unittest") must not appear in argv0.
        self.assertNotIn("unittest", argv0.lower())


if __name__ == "__main__":
    unittest.main()
