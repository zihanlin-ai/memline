/**
 * memline session recall for opencode.
 *
 * opencode splits the job across two plugin surfaces, so this uses both:
 *
 *   event          observes `session.created` and `session.compacted`. These are
 *                  real signals, but the hook has no output channel, so it can
 *                  only mark what the session owes.
 *   chat.message   has `output.parts`, so this is where the text actually goes.
 *                  It injects whatever the event hook queued, on the next
 *                  message.
 *
 * The two moments want different content:
 *
 *   session.created   -> window mode: memories from the last N days, since
 *                        nothing about this conversation exists yet.
 *   session.compacted -> session mode: everything THIS session wrote, oldest
 *                        first, because compaction discards precisely what this
 *                        conversation established.
 *
 * Recall policy lives in ../common/session_recall.py, shared with the Claude
 * Code and codex integrations, so the window, the cap and which fields survive
 * are decided once. `memline start` returns ~29 KB for 25 entries because of its
 * metadata; the helper keeps only the text plus the date and writer, and trims
 * to a character budget.
 *
 * Failures are swallowed: a recall miss must never block a message.
 *
 * Install: symlink (or copy) to <workspace>/.opencode/plugin/session_recall.js.
 * Plugins load at opencode startup: restart a long-lived `opencode serve`.
 */

import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { fileURLToPath } from "node:url"
import { appendFileSync } from "node:fs"
import path from "node:path"

const run = promisify(execFile)
const HERE = path.dirname(fileURLToPath(import.meta.url))
const HELPER = path.join(HERE, "..", "common", "session_recall.py")

// Same invocation log the bash adapters write. Without it there is no way to
// tell "plugin never loaded" from "loaded but the hook never fired" from
// "fired and threw" -- three failures that look identical from the outside.
// The load line matters most: plugins load once at startup, so a long-lived
// `opencode serve` silently runs whatever set of plugins existed when it
// booted, however many times the files change afterwards.
const LOG =
  process.env.MEMLINE_HOOK_LOG ??
  "/workspace/.agent-memory/store/hook-invocations.log"

function log(fields) {
  try {
    const parts = Object.entries(fields).map(([k, v]) => `${k}=${v}`)
    appendFileSync(LOG, `${new Date().toISOString()} harness=opencode ${parts.join(" ")}\n`)
  } catch {
    /* logging must never break a session */
  }
}

// sessionID -> "window" | "session", set by the event hook, drained by
// chat.message. A session start and a later compaction both queue work.
const pending = new Map()

async function recall(args) {
  try {
    const { stdout } = await run("python3", [HELPER, ...args], {
      timeout: 30_000,
      maxBuffer: 8 * 1024 * 1024,
    })
    return stdout && stdout.trim() ? stdout : null
  } catch {
    return null
  }
}

// opencode validates part ids against a schema that requires the "prt" prefix
// ("Expected a string starting with \"prt\""). The check runs when the message is
// saved, AFTER the hook returns, so a bad id cannot be caught here -- it rejects
// the whole user message, dropping the user's own text along with the injection.
// A recall hook that silently eats messages is far worse than one that no-ops.
const ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
function partID() {
  let s = ""
  for (let i = 0; i < 26; i++) s += ALPHABET[Math.floor(Math.random() * ALPHABET.length)]
  return `prt_${s}`
}

function sessionOf(event) {
  const p = event?.properties ?? {}
  return (
    p.sessionID ?? p.sessionId ?? p.session?.id ?? p.info?.id ?? p.id ?? null
  )
}

export default async () => {
  log({ phase: "load", pid: process.pid })
  return {
  event: async ({ event }) => {
    const type = event?.type
    if (type !== "session.created" && type !== "session.compacted") return
    const sessionID = sessionOf(event)
    if (!sessionID) {
      // the event fired but carried no id under any known key: queueing is
      // impossible, so record the shape rather than failing silently
      log({ phase: "event", type, sessionID: "MISSING", keys: Object.keys(event?.properties ?? {}).join(",") || "-" })
      return
    }
    log({ phase: "event", type, sessionID })
    // a compaction supersedes a not-yet-drained start: what was lost matters
    // more than the generic window
    pending.set(sessionID, type === "session.compacted" ? "session" : "window")
  },

  "chat.message": async (input, output) => {
    const sessionID = input?.sessionID
    if (!sessionID) return
    const mode = pending.get(sessionID)
    if (!mode) return
    pending.delete(sessionID)

    const text =
      mode === "session"
        ? await recall(["--mode", "session", "--session-id", sessionID])
        : await recall(["--mode", "window"])
    if (!text) {
      log({ phase: "inject", mode, sessionID, bytes: "0-EMPTY" })
      return
    }

    try {
      // `synthetic` marks text the user did not type. It is a real TextPart
      // field -- declared in @opencode-ai/sdk gen/types.gen.d.ts, though not in
      // the narrower view the plugin package re-exports -- and opencode uses it
      // for its own injections (plan mode pushes a synthetic part the same way),
      // which also confirms synthetic parts still reach the model.
      output.parts.unshift({
        id: partID(),
        sessionID,
        messageID: output.message?.id ?? "",
        type: "text",
        text,
        synthetic: true,
      })
      log({ phase: "inject", mode, sessionID, bytes: text.length })
    } catch (err) {
      /* part shape rejected: skip rather than break the message */
      log({ phase: "inject", mode, sessionID, bytes: "REJECTED", err: String(err?.message ?? err).slice(0, 120) })
    }
  },
  }
}
