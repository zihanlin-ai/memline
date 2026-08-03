/**
 * memline writer-context bridge for opencode.
 *
 * opencode does not export its session identity to child processes, so
 * `memline add` run from the shell tool has no hard signal to attribute the
 * write to. This plugin injects that identity into the environment of every
 * shell command opencode spawns:
 *
 *   OPENCODE_SESSION_ID -> memline session_id / run_id
 *   OPENCODE_CALL_ID    -> tool call that issued the command (debug only)
 *
 * memline maps OPENCODE_SESSION_ID to the writer session and pins the
 * writer source (and therefore agent_id) to "opencode" -- deliberately the
 * harness, not the underlying model, so memories stay comparable across model
 * switches. See writer_context.py in the memline package.
 *
 * The PTY path triggers shell.env without a sessionID; there memline falls
 * back to ancestor-process detection, which still yields source "opencode".
 *
 * Install: symlink (or copy) this file to <workspace>/.opencode/plugin/memline.js.
 * Plugins load at opencode startup: restart a long-lived `opencode serve`
 * after installing or editing it.
 */
export default async () => ({
  "shell.env": async (input, output) => {
    if (input?.sessionID) output.env.OPENCODE_SESSION_ID = input.sessionID
    if (input?.callID) output.env.OPENCODE_CALL_ID = input.callID
  },
})
