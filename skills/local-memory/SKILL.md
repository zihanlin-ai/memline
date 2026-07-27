---
name: local-memory
description: Use the `mem0-local` CLI for workspace-local memory search, add, get, list, update, delete, and audited historical memory import. Trigger when the user asks to search, add, migrate, audit, troubleshoot, or inspect local memory backed by the workspace Mem0/Qdrant store, or asks for an end-of-session handoff review (过期审查 / 收尾审查 / handoff review).
---

# Local Memory

## Core Usage

Call `mem0-local` directly; it is installed on PATH.

```bash
mem0-local --help
mem0-local <command> --help

mem0-local start                                     # session bootstrap: recall recently ingested memories (last 1d)
mem0-local add "accurate memory text"
mem0-local add "newer fact" --supersedes <old_id>   # when this write knowingly replaces an old entry
mem0-local search "query"                            # retired entries excluded by default
mem0-local search "query" --include-superseded       # history digs: include retired entries
mem0-local get <memory_id>
mem0-local update <memory_id> "updated memory text"
mem0-local delete <memory_id> --force   # destructive ops confirm on a TTY; non-interactive agents must pass --force (preview with --dry-run)
mem0-local stale protect <memory_id> --reason "..." [--days 30]  # bounded displacement-only suppression
mem0-local stale protected list
mem0-local stale unprotect <memory_id>
mem0-local entity list --contains "<text>"
```

In Codex/Claude contexts, output defaults to agent-readable JSON. Use `--json` explicitly only for portability, or use `--output text` / `--output table` for human-readable output.

## Core Rules

- The stack is self-healing: any `mem0-local` command auto-starts the daemon when it is down (~15-30s once after a reboot; later calls are fast), and the daemon in turn starts the local qdrant server if needed. No manual `daemon status`/`daemon start` or `qdrantctl.sh start` is required at session start. Opt out with `MEM0_LOCAL_NO_AUTOSTART=1`; `MEM0_LOCAL_NO_DAEMON=1` still forces the direct path.
- Auto-start is namespace-guarded: from an isolated sandbox context (host qdrant unreachable) the CLI refuses to spawn the daemon or a duplicate qdrant and prints a hint to run `mem0-local daemon start` from a normal shell instead. If memory commands report qdrant connection errors in such a context, that is the sandbox, not the store.
- Write small, frequent, single-fact entries — not big multi-fact paragraphs. One `add` per atomic fact, called often, beats one long dense entry. This matches Mem0's one-fact-per-memory retrieval model and avoids the LLM-extraction backend truncating long entries. Raw writes enforce a hard length cap (default 600 chars): an over-cap `add` errors before touching the store — split it into multiple single-fact adds (or use `--infer` extraction for genuinely long conversational input). `update` may keep or shrink an over-cap legacy entry but cannot grow it past the cap.
- Plain-text `add` stores the exact text verbatim by default (raw mode): synchronous, ~1s, returns the memory id, exact-hash dedup, near-duplicate annotation, entity-linked. Because no LLM normalizes the text anymore, the writer must produce entries that are atomic, self-contained (no pronouns or session-relative references), dated, and rich in exact identifiers.
- LLM extraction still runs for `--messages`/`--file` input (conversations genuinely need it) or when `--infer` is passed explicitly. Extraction adds are queued asynchronously: they return `{event_id, status: queued}` in about a second; do not wait or poll routinely. Use `--infer --wait` to confirm inline (extraction errors then surface as a CLI error, exit code 1). Check progress with `event status <event_id>`; empty `results` on completion means dedup or nothing extractable.
- Async extraction failures are retried up to 3 times, then marked failed and announced as a stderr warning banner on later hygiene-command invocations (`review`/`stale`/`event`/`status`; routine `add`/`search`/`start` stay quiet). On seeing the banner: `event list --status failed`, then `event retry <event_id>` (requeues the original input) or `event ack <id>|--all` (dismisses). Queued events survive daemon restarts (persistent queue with crash replay).
- Raw storage is the default because agent-authored entries are already atomic facts and verbatim fidelity beats LLM rewriting for paths, hosts, commands, and dates (policy updated 2026-07-16; the older infer-by-default rule is superseded). After extraction (`--infer`/`--messages`) completes, inspect the stored memories for accuracy; correct distortions promptly with `update` or `delete --force` + re-add.
- Do not manually pass source, agent id, session id, timestamps, schema fields, or output formatting for routine use. `add`, `search`, and `update` automatically include timestamps, writer/session identity, schema metadata, and agent-readable JSON output in agent contexts.
- `add`, `update`, and `delete` automatically append external audit rows to `.agent-memory/manifests/live-YYYY-MM.jsonl` by month. Agents do not need to manage this manifest; use it for human audit of raw inputs, metadata, Mem0 results, memory ids, and timings.
- In agent contexts, `add`, `search`, `list`, `get`, `update`, `delete`, and `status` return JSON by default unless `--output ...` is explicitly passed. Agents should not add formatting flags unless they need a non-default format.
- `search` and `get` return the stored timestamps.
- Keep `search` as pure semantic retrieval: pass a query, optionally `--top-k` or `--rerank`, and do not use it for agent/session/time scoping. Default retrieval is hybrid (vector + BM25); `--keyword` switches to pure BM25 term matching (exact identifiers, paths, error strings), and `--fields memory,score` projects result fields. The local `--threshold` default is 0.1 (official CLI: 0.3) on purpose — local hybrid scores are distributed lower; do not "align" it to 0.3.
- Raw adds hash-dedup exact re-fires (event `NONE` with `duplicate_of`) and annotate semantic near-duplicates with `near_duplicate_of`/`near_duplicate_score` (cosine >= 0.95) without skipping the store — review the hint and `delete --force` the redundant copy if it truly duplicates.
- Lifecycle hygiene is deliberately invisible during task execution. Background judges quietly mark entries for later; `suspected_stale`/`suspicions` fields on search hits are advisory (prefer checking whether a newer answer exists); stderr suspicion banners require NO action mid-task (and only print on `review`/`stale`/`event`/`status` anyway). When a write knowingly replaces an evolving old fact, `add "..." --supersedes <old_id>` retires the old entry in the same step — that is the only lifecycle action that belongs inside a task. Everything else happens only when the user asks for a session handoff (e.g. "做一次过期审查"): first filter and personally maintain every memory written by the detected current session, then run `mem0-local review --wait` to process the remaining judge flags and displacement pairs. Read [handoff-review.md](references/handoff-review.md) and follow its complete preflight → review → repeat-until-clean workflow.
- The session-handoff banner is the one stderr banner addressed to the current agent: once a session's live adds exceed the threshold (config `[memory].session_add_alert_threshold`, default 200, 0 disables), every `mem0-local` command from that session prints "this session has accumulated N memory add(s)". On seeing it, tell the user the session has accumulated many memory writes and suggest a handoff; run the handoff review only after they agree. Other sessions are unaffected — the counter is per writer session (`store/session-stats.db`).
- `stale protect` is not a permanent pin: it suppresses only displacement candidate judging, defaults to 30 days (maximum 90), requires a 1-500 character reason plus recorded agent/session identity, and never skips necessity/correctness/safety. The core setter requires the latest three opening displacement suspicions for the current text version to all be dismissed and to have distinct `new_id` values; any other newer disposition interrupts the run. There is no force bypass. Changing or invalidating the memory clears protection automatically.
- Disposition authority is enforced by the CLI for `stale confirm` / `dismiss` / `merge` / `ttl`: same-session pairs are allowed; cross-session pairs require a default-No interactive confirmation or explicit user-approved `--force` non-interactively (`ttl_expiry` is exempt). A dismissed exact text-versioned pair is permanent and cannot be reopened; only a different future `new_id` can create new evidence.
- `delete` and `entity delete` are guarded: they prompt for confirmation on a TTY and refuse in non-interactive contexts without `--force`; `--dry-run` previews what would be deleted (for `delete --all --dry-run`, without needing `--force`).
- `entity list/delete` manage the local entity graph (spaCy-extracted entities linked to memories). Deleting an entity row never touches the memories themselves; use it to prune junk or stale entities. `entity delete` is audited to the live manifest like other mutations.
- Use `list --filter ...` only when the user asks to enumerate/audit memories by structured fields such as time range, writer, session, source, or import batch. See [commands.md](references/commands.md) for field details.
- To enumerate memories in a date/time range, use `list --filter` with a JSON range filter instead of semantic `search`; see [commands.md](references/commands.md) for `created_at` versus `ingested_at` examples.
- Prefer English search terms, exact paths, commands, ports, model names, and environment variables. If the user asks in Chinese, keep the Chinese intent but add the likely English entities/keywords.
- `update` preserves original writer/session scope and automatically records the current updater identity. Agents should only supply the corrected memory text and optional human-meaningful reason metadata.
- At session start, after reading `.agent-memory/MEMORY.md`, list memories ingested in the last 1 day with `since="$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(days=1)).isoformat())')"; mem0-local list --filter "{\"ingested_at\":{\"gte\":\"$since\"}}" --page-size 100`; then use semantic `search` for task-specific recall.
- Never print or read the local secret env file.
- Memory entries must never contain plaintext credentials (passwords, tokens, private keys, one-time auth values) — reference the secret's location instead (the env file, a chmod600 secret file, hosts.yaml). If an existing entry holds a plaintext secret, redact it in place with `update <id> "<same text, secret replaced by a pointer>"`: this scrubs the value from the store while preserving the id, entity links, and history, and records the redaction in the manifest. Verify with `search "<secret literal>" --include-superseded` returning nothing.
- Do not use `delete --all --force` unless the user explicitly requests a scoped wipe.

## Progressive References

- For the full command list, time-range listing, and common examples, read [commands.md](references/commands.md).
- For real paths, PATH/symlink details, reusable package location, workspace config, store layout, Qdrant lock behavior, missing command issues, or rollback checks, read [troubleshooting.md](references/troubleshooting.md).
- For historical Markdown ledger migration policy, timestamp-source rules, and dry-run/import audit guidance, read [imports.md](references/imports.md).
- For the end-of-session handoff workflow (current-session preflight filtering, proactive correction/retirement, review output anatomy, disposition commands, protection list, authority rules, and the final clean-pass requirement), read [handoff-review.md](references/handoff-review.md) — only at handoff time, never mid-task.
- For the staleness/supersession design (invalidation data model, background judge, disposition authority, edge-case state machine), the review false-positive rubric, and how to retrace a conclusion's full lineage, read [staleness-design.md](references/staleness-design.md).
