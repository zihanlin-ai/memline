"""Who is writing this memory. Decided from hard signals, never from content.

Every memory carries the identity of the agent and session that wrote it, and
that attribution has one rule the code enforces at every step: the text being
written must not be able to influence it. So detection reads identity
environment variables, the AI_AGENT tag, and ancestor executable *basenames*
only — argv[0], never a full command line, because a full command line can
contain the very memory text under attribution, and an attacker-shaped string
must not get to choose who the store says wrote it.

This lived inside the CLI for a long time, which hid what it is: a complete
subsystem with its own contract, used by every write path and testable with
nothing but a fake /proc and a patched environ.

The identity table is ordered for nested agents: an `opencode` started from a
claude shell inherits CLAUDECODE, so sources whose identity vars are injected
per shell invocation come first — the innermost agent wins.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def read_proc_ppid(pid: int) -> int | None:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def read_proc_argv0(pid: int) -> str:
    """Return only argv[0] (the executable path) for a pid.

    Reads just the first NUL-delimited field of /proc/<pid>/cmdline, so
    process-based agent detection can never see user-supplied argv content
    such as the memory text being written.
    """
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    first = raw.split(b"\x00", 1)[0]
    return first.decode(errors="replace").strip()


def ancestor_exe_names(limit: int = 10) -> list[str]:
    """Basenames of ancestor process executables (argv[0] only).

    Excludes the current process and never inspects full command lines, so the
    memory content being written cannot be mistaken for an agent identity.
    """
    names: list[str] = []
    pid = os.getpid()
    for _ in range(limit):
        ppid = read_proc_ppid(pid)
        if not ppid or ppid <= 1 or ppid == pid:
            break
        pid = ppid
        argv0 = read_proc_argv0(pid)
        if argv0:
            names.append(os.path.basename(argv0).lower())
    return names


# Ordered agent identity table: (source, hard identity env vars, argv0 markers).
# Detection uses these signals only -- never the memory content being written.
# Order matters when agents are nested (e.g. `opencode` started from a claude
# shell inherits CLAUDECODE): the innermost agent must win, so sources whose
# identity vars are injected per shell invocation come first.
_AGENT_IDENTITY: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        # OPENCODE_SESSION_ID / OPENCODE_CALL_ID are injected per shell call by
        # the memline opencode plugin (.opencode/plugin/memline.js).
        "opencode",
        ("OPENCODE_SESSION_ID", "OPENCODE_CALL_ID"),
        ("opencode",),
    ),
    (
        "codex",
        ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_MANAGED_PACKAGE_ROOT"),
        ("codex",),
    ),
    (
        "claude",
        (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDECODE_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDECODE",
        ),
        ("claude",),
    ),
)


def detect_agent_source() -> str | None:
    """Identify the writing agent from hard signals only.

    Priority: per-agent identity env vars, then the generic AI_AGENT/AGENT tag,
    then ancestor executable names. Never inspects the memory content or the
    full command line, so what is being written cannot change the attribution.
    """
    # 1. Dedicated per-agent identity env vars (hardest signal).
    for source, env_names, _markers in _AGENT_IDENTITY:
        if first_env(*env_names):
            return source
    # 2. Generic agent tag, e.g. AI_AGENT="claude-code_2-1-205_agent" -> "claude".
    tag = first_env("AI_AGENT", "AGENT")
    if tag:
        token = re.split(r"[^A-Za-z0-9]+", tag.strip().lower(), maxsplit=1)[0]
        if token:
            return token
    # 3. Last resort: ancestor executable basenames (content-safe, argv[0] only).
    names = ancestor_exe_names()
    for source, _env_names, markers in _AGENT_IDENTITY:
        if any(marker in name for name in names for marker in markers):
            return source
    return None


def detect_writer_context() -> dict[str, str]:
    """Best-effort local caller detection for audit metadata.

    Both source and session_id come from hard signals only -- explicit MEM0_*
    overrides, per-agent identity env vars, the AI_AGENT tag, and ancestor
    executable names. The memory content being written is never inspected.
    """
    source = first_env("MEMLINE_SOURCE", "MEM0_SOURCE", "AGENT_SOURCE", "AI_AGENT_SOURCE")
    session_id = first_env(
        "MEMLINE_SESSION_ID",
        "MEM0_SESSION_ID",
        "AGENT_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE_SESSION_ID",
    )
    if not source:
        source = detect_agent_source()

    context: dict[str, str] = {}
    if source:
        context["source"] = source
    if session_id:
        context["session_id"] = session_id
    return context
