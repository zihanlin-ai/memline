#!/usr/bin/env python3
"""Shared session recall for every harness integration.

Emits recalled memories as plain text on stdout; each harness adapter wraps that
text in its own envelope, so recall policy lives here once instead of drifting
across three copies.

Two modes, because the two moments need different content:

  window  (default)  Memories ingested in the last N days, from any session.
                     For a fresh session start, where nothing about this
                     conversation exists yet.

  session            Everything THIS session wrote, oldest first. For after a
                     context compaction, where what was lost is precisely what
                     this conversation established. A time window is the wrong
                     filter there: the session may be older than the window, and
                     other sessions' memories are noise.

Why the payload is compacted: `memline start` returns its full record shape
(id, hash, metadata, session ids, timestamps) -- roughly 29 KB for 25 entries
while the memory text is a fraction of that. A hook pays that cost on every
firing, relevant or not, and codex additionally enforces an
`additionalContextLimit`. Only the text, plus the date and writer needed to
judge it, survive.

Output is capped at MEMLINE_RECALL_MAX_CHARS (default 10000). Claude Code
truncates `additionalContext` beyond 10k characters into a file with only a
preview left inline, and codex enforces its own `additionalContextLimit`, so a
payload that overflows silently loses most of itself. When the cap bites, the
oldest entries are dropped -- recent memories are the ones bearing on current
work -- and the header says how many were left out.

Exit codes: always 0. Text on stdout when there is something to say, nothing
otherwise. Recall must never be the reason a session fails to start or a
compaction fails to complete.

Env: MEMLINE_START_DAYS (1), MEMLINE_START_LIMIT (25), MEMLINE_BIN (memline),
     MEMLINE_SESSION_LIMIT (200), MEMLINE_RECALL_MAX_CHARS (10000).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys


def _run(binary: str, args: list[str]) -> dict | list | None:
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=30
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _entries(payload) -> list[dict]:
    """memline wraps results differently per subcommand; find the record list."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("results", "memories", "items", "data"):
            if key in payload:
                found = _entries(payload[key])
                if found:
                    return found
    return []


def _format(entries: list[dict]) -> list[str]:
    lines = []
    for entry in entries:
        text = (entry.get("memory") or "").strip()
        if not text:
            continue
        when = (entry.get("created_at") or "")[:10]
        who = entry.get("agent_id") or (entry.get("metadata") or {}).get("source") or "?"
        lines.append(f"- [{when} {who}] {text}")
    return lines


def _fit(lines: list[str], header_len: int, budget: int, drop: str) -> tuple[list[str], int]:
    """Trim until the rendered payload fits, always discarding the oldest.

    Which end that is depends on the mode: `memline start` returns newest first,
    while session mode is sorted oldest first for readability. Passing the wrong
    end silently throws away the most relevant memories, which is the opposite
    of what a budget should do.
    """
    dropped = 0
    size = header_len + 2 + sum(len(x) + 1 for x in lines)
    while lines and size > budget:
        removed = lines.pop() if drop == "tail" else lines.pop(0)
        size -= len(removed) + 1
        dropped += 1
    return lines, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("window", "session"), default="window")
    ap.add_argument("--session-id", default="")
    args = ap.parse_args()

    binary = os.environ.get("MEMLINE_BIN", "memline")
    if shutil.which(binary) is None:
        return 0

    if args.mode == "session":
        session_id = args.session_id or os.environ.get("MEMLINE_SESSION_ID", "")
        if not session_id:
            return 0
        limit = os.environ.get("MEMLINE_SESSION_LIMIT", "200")
        payload = _run(
            binary,
            ["list", "--filter", f"run_id={session_id}", "--page-size", str(limit),
             "-o", "json"],
        )
        entries = _entries(payload)
        # oldest first: after a compaction the useful reading order is the order
        # in which this conversation established things
        entries.sort(key=lambda e: e.get("created_at") or "")
        lines = _format(entries)
        # a session that compacts before writing anything has nothing of its own
        # to restore; the recent window is more useful than silence
        if lines:
            mode, drop = "session", "head"
        else:
            args.mode = "window"
    if args.mode == "window":
        days = os.environ.get("MEMLINE_START_DAYS", "1")
        limit = os.environ.get("MEMLINE_START_LIMIT", "25")
        payload = _run(binary, ["start", "--days", str(days), "--limit", str(limit)])
        lines = _format(_entries(payload))   # newest first, as memline returns them
        if not lines:
            return 0
        mode, drop = "window", "tail"

    budget = int(os.environ.get("MEMLINE_RECALL_MAX_CHARS", "10000"))
    # reserve room for the longest header this can produce, so the count in the
    # header always matches the number of lines actually printed
    reserve = 260
    lines, dropped = _fit(lines, reserve, budget, drop)
    if not lines:
        return 0

    if mode == "session":
        header = (
            f"{len(lines)} memories this session wrote, restored by memline after "
            "context compaction (oldest first). These are the durable facts from "
            "the part of the conversation that was summarised away."
        )
    else:
        days = os.environ.get("MEMLINE_START_DAYS", "1")
        header = (
            f"{len(lines)} memories from the last {days} day(s), recalled by memline "
            "at session start. Use `memline search <query>` for task-specific recall."
        )
    if dropped:
        header += f" ({dropped} older entries omitted to fit the context budget.)"

    sys.stdout.write(header + "\n\n" + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
