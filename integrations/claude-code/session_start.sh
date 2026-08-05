#!/usr/bin/env bash
#
# memline session bootstrap for Claude Code.
#
# Claude Code fires SessionStart with a `source` of startup, resume, clear,
# compact or fork, and the source decides what to recall:
#
#   compact                  -> everything THIS session wrote, oldest first.
#                               Compaction discards precisely what this
#                               conversation established; a time window would
#                               return other sessions' noise instead.
#   startup/resume/clear/fork -> the recent-window recall.
#
# The script handles all five, but the registered matcher is `startup|compact`
# on purpose: inject only when the context is empty or was destroyed, never when
# it is inherited. Claude Code keeps injected text in the session transcript and
# re-runs SessionStart on resume and fork, so recalling there would put the same
# payload in context twice. `clear` does wipe the context and would qualify, but
# is excluded by explicit preference.
#
# SessionStart is the only injection point for the post-compaction case:
# PostCompact fires too, but it has no decision control and no additionalContext
# -- it is observational, for logging and cleanup. A recall hook registered
# there would run and silently do nothing.
#
# Recall policy lives in ../common/session_recall.py, shared with the codex and
# opencode integrations. This file is only the Claude Code envelope.
#
# Input: hook JSON on stdin (session_id, source, cwd, ...). Absent or
# unparseable stdin degrades to window mode rather than failing.
#
# Output contract: one JSON object on stdout; Claude Code appends
# `.hookSpecificOutput.additionalContext` to the session context. Anything else
# printed corrupts the envelope, so every failure path exits silently.
#
# Install: register in .claude/settings.json under hooks.SessionStart:
#   bash <memline>/integrations/claude-code/session_start.sh
# Hooks are read at session start: restart Claude Code after editing.

set -uo pipefail

# --- observability -----------------------------------------------------------
# Everything below was designed from documentation and tested against a stdin
# payload we constructed ourselves, which cannot falsify a wrong belief about
# what the harness actually sends. This records each real invocation so the
# assumptions can be checked against reality instead of restated.
# Log only; never stdout, which carries the contract.
_log() {
  local logf="${MEMLINE_HOOK_LOG:-/workspace/.agent-memory/store/hook-invocations.log}"
  mkdir -p "$(dirname "$logf")" 2>/dev/null || return 0
  [ -f "$logf" ] && [ "$(wc -c <"$logf" 2>/dev/null || echo 0)" -gt 262144 ] && tail -c 131072 "$logf" > "$logf.tmp" 2>/dev/null && mv "$logf.tmp" "$logf" 2>/dev/null
  printf '%s harness=%s stdin_bytes=%s stdin=%s mode=%s emitted_bytes=%s\n' \
    "$(date -Iseconds)" "$1" "${#2}" "$(printf '%s' "$2" | tr -d '\n' | cut -c1-400)" "$3" "$4" >> "$logf" 2>/dev/null
  return 0
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

payload=""
if [ ! -t 0 ]; then
  payload=$(timeout 5 cat 2>/dev/null || true)
fi

read -r mode session_id <<<"$(
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
source = str(d.get("source") or "")
mode = "session" if source == "compact" else "window"
print(mode, d.get("session_id") or "")
' 2>/dev/null || echo "window "
)"
mode="${mode:-window}"

if [ "$mode" = "session" ] && [ -n "${session_id:-}" ]; then
  recall=$(python3 "$HERE/../common/session_recall.py" --mode session --session-id "$session_id" 2>/dev/null) || exit 0
else
  recall=$(python3 "$HERE/../common/session_recall.py" --mode window 2>/dev/null) || exit 0
fi
[ -n "${recall//[[:space:]]/}" ] || { _log "claude-code" "$payload" "${mode:-window}" "0-EMPTY"; exit 0; }

_log "claude-code" "$payload" "${mode:-window}" "${#recall}"

printf '%s' "$recall" | python3 -c '
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": sys.stdin.read(),
    }
}))
' 2>/dev/null || exit 0
