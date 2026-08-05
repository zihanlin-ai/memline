#!/usr/bin/env bash
#
# memline session bootstrap for Codex.
#
# Codex uses the same hooks.json schema as Claude Code (event -> matcher ->
# {type: command}) and supports SessionStart, so this mirrors the Claude Code
# adapter and shares the same recall policy in ../common/session_recall.py.
#
# Output contract: the codex binary carries an `additionalContext` key and an
# `additionalContextLimit`, but does not expose the exact envelope shape the way
# Claude Code documents it. Both the nested Claude-compatible form and a flat
# top-level key are emitted; a consumer reading either one finds the text, and
# unknown keys are ignored. Revisit once codex documents the schema.
#
# The `additionalContextLimit` is the reason the shared helper compacts the
# payload rather than passing `memline start` output through unchanged.
#
# Codex can fire SessionStart with a `source` of startup, resume, clear or
# compact. The shipped hooks register only startup and compact: a resumed
# transcript already contains the original injected developer message, so
# recalling again would duplicate it; clear is excluded by explicit preference.
# The adapter still parses every source defensively, selecting this session's
# own memories only for compact and the recent window otherwise. SessionStart
# is the injection point for both registered cases -- PostCompact exists but is
# observational, with no additionalContext.
#
# Install: ship as a plugin hooks.json, or add to ~/.codex/config.toml. Codex
# requires persisted hook trust before an enabled hook will run -- unlike Claude
# Code, writing the config is not sufficient on its own.

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
mode = "session" if str(d.get("source") or "") == "compact" else "window"
print(mode, d.get("session_id") or "")
' 2>/dev/null || echo "window "
)"
if [ "${mode:-window}" = "session" ] && [ -n "${session_id:-}" ]; then
  recall=$(python3 "$HERE/../common/session_recall.py" --mode session --session-id "$session_id" 2>/dev/null) || exit 0
else
  recall=$(python3 "$HERE/../common/session_recall.py" --mode window 2>/dev/null) || exit 0
fi
[ -n "${recall//[[:space:]]/}" ] || { _log "codex" "$payload" "${mode:-window}" "0-EMPTY"; exit 0; }

_log "codex" "$payload" "${mode:-window}" "${#recall}"

printf '%s' "$recall" | python3 -c '
import json, sys
text = sys.stdin.read()
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }
}))
' 2>/dev/null || exit 0
