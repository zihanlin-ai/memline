# Mem0 Local Command Reference

Use built-in help first:

```bash
memline --help
memline add --help
memline search --help
```

## Core Commands

Routine agents should use only the simple forms below. The CLI handles writer
identity, session id, timestamps, schema fields, and JSON output automatically.

```bash
memline status
memline add "memory text"
memline search "query"
memline list
memline get <memory_id>
memline update <memory_id> "updated memory text"
memline delete <memory_id> --force
memline entity list --contains "<text>"
```

When called from Codex/Claude contexts, output defaults to agent-readable JSON.
Use `--json` only when portability matters, and use `--output text` or
`--output table` for human-readable output.

## Start (session bootstrap)

```bash
memline start                 # recall memories ingested in the last 1 day, newest first
memline start --days 3        # widen the recall window
memline start --limit 200
```

`start` is the one-liner session bootstrap: it lists recently ingested
memories so a new session picks up recent context, then use semantic
`search` for task-specific recall. Equivalent to a `list --filter` on an
`ingested_at` range but with no JSON to hand-write.

## Add

Normal add automatically records `created_at`, `ledger_timestamp`, `ingested_at`,
and best-effort writer context. When available, the CLI stores
schema-v2 fields `metadata.source`, `metadata.session_id`,
`metadata.writer_agent_id`, `metadata.origin`, and
`metadata.memory_schema_version`, and uses source/session as default top-level
`agent_id` and `run_id`.

If no agent context is detectable, the default is `source=manual`,
`agent_id=manual`, and `session_id/run_id=manual-session`.

Agents should not pass source/session/timestamp/schema metadata for routine
adds. The only required work is writing accurate, atomic memory text.

```bash
memline add "memory text"
```

Every successful live `add` appends one external audit row to
`.agent-memory/manifests/live-YYYY-MM.jsonl`. The row includes the raw input
content, infer mode, automatic metadata, scope, Mem0 result, memory ids/result
memories, timings, and payload hash. Agents do not need extra flags for this.

Override auto-detection only when it is missing or wrong:

```bash
memline add "memory text" --metadata source=agent-memory-ledger --metadata session_id=manual-import
```

Plain-text adds store the exact wording verbatim by default (raw mode, since
2026-07-16). LLM extraction runs for `--messages`/`--file` input or when
`--infer` is passed explicitly; extraction adds queue asynchronously and
return an `event_id` (see `event list/status/retry/ack`).

Raw writes have a hard length cap (default 600 chars, `[memory].max_raw_text_chars`
in config.toml). An over-cap `add` errors before touching the store: split the
content into multiple single-fact adds, or pass `--infer` for extraction.
`update` may keep or shrink an over-cap legacy entry (redaction is never
blocked) but cannot grow it past the cap.

```bash
memline add "exact ledger entry" --metadata source=agent-memory-ledger
memline add --infer "dense multi-fact paragraph worth splitting into atomic memories"
```

Historical imports may override event time:

```bash
memline add "old memory text" --timestamp "2026-05-18T00:00:15+08:00"
```

## Search

```bash
memline search "proxy benchmark"
memline search "proxy benchmark" --rerank
```

`search` returns `created_at`, `updated_at`, and metadata timestamps when present.

Keep `search` as pure semantic retrieval. Do not use it for agent/session/time
scoping; use `list --filter ...` for structured enumeration and audit queries.

Search is more reliable when the query includes English terms and exact
technical identifiers, because inferred memories are often stored in English.
For Chinese user questions, translate the core intent into English and keep
important literals unchanged:

```bash
memline search "service fully ready proxy 7.246.46.187:7000 P 9000 D ports 9100 9101 9102 9103 /v1/models"
memline search "ACS Bench provider endpoint 7.246.46.187:9000 pangu_ultra_moe"
memline search "baseline_all_features VLLM_TORCH_PROFILER_DIR VLLM_TORCH_PROFILER_RECORD_SHAPES"
```

Use `--rerank` only for deliberate experiments; in the current local setup it
can be much slower than base search.

## List by Scope and Time Range

Use `list`, not semantic `search`, when the user asks to enumerate all memories
from a scope or date/time range. Use `--filter` for agent/session/time filters.

Use top-level fields for ordinary writer/session scope:

```bash
memline list --filter agent_id=codex --page-size 100
memline list --filter run_id=019eb447-4302-7f32-9eeb-66bfbe5f7d51 --page-size 100
memline list --filter run_id=legacy-codex --page-size 100
memline list --filter run_id=ses_b8d2ac181351976b11df6be5bb --page-size 100
memline list \
  --filter '{"agent_id":"codex","run_id":"019eb447-4302-7f32-9eeb-66bfbe5f7d51"}' \
  --page-size 100
```

## Optional Daemon

If repeated memory commands are slow, start the optional local daemon:

```bash
memline daemon start
memline daemon status
memline daemon stop
```

The daemon is a local Unix-socket process under the workspace memory store. It
keeps the Mem0/FastEmbed/ONNX client warm, so `get`, `list`, base `search`, and
raw writes avoid per-command cold start. Commands fall back to the direct path
when the daemon is not running. To deliberately use the direct path for
debugging, stop the daemon first and then run:

```bash
MEMLINE_NO_DAEMON=1 memline get <memory_id>
```

Use metadata fields such as `source` or `session_id` for import/audit queries
or when the metadata-level value itself is the intended filter:

```bash
memline list --filter source=agent-memory-ledger --page-size 100
memline list --filter session_id=019eb447-4302-7f32-9eeb-66bfbe5f7d51 --page-size 100
memline list --filter '{"writer_agent_id":"opencode","origin":"ledger_import"}' --page-size 100
memline list --filter origin=ledger_import --page-size 100
memline list --filter memory_schema_version=2 --page-size 100
```

All existing memories were backfilled to schema v2. The git-managed audit
manifest lives under `.agent-memory/manifests/metadata-backfill-*.jsonl`.

Use JSON range filters with ISO-8601 timestamps for time ranges.

Use `created_at` for the memory's event timestamp. For ordinary `add`, this is
usually the write time; for historical imports it may be the original ledger
time.

```bash
memline list \
  --filter '{"created_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

Use `ingested_at` for the actual time the memory entered Mem0/Qdrant.

```bash
memline list \
  --filter '{"ingested_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

Combine scope filters with time filters by putting them in the same JSON object:

```bash
memline list \
  --filter '{"agent_id":"claude","ingested_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

## Get, List, Update, Delete

```bash
memline get <memory_id>
memline list --filter agent_id=codex
memline list --filter run_id=<session_id>
memline update <memory_id> "new text" --metadata reason=correction
memline delete <memory_id> --force
```

Destructive commands (`delete`, `entity delete`) confirm interactively on a
TTY; in non-interactive agent contexts they refuse without `--force`. Use
`--dry-run` first to preview what would be deleted (`delete --all --dry-run`
needs no `--force`).

`update` preserves the original writer scope (`agent_id`/`run_id` and
`metadata.writer_agent_id`/`metadata.session_id`) plus original `created_at` and
`ledger_timestamp`. It updates Mem0's `updated_at` and writes
`updated_by_cli_at`, `last_updated_by_agent_id`, and
`last_updated_session_id`.

If Claude wrote a memory and Codex corrects it, the memory remains scoped to
Claude as the original writer; the metadata records Codex as the latest updater.
The update also appends a live audit row containing the existing memory snapshot,
replacement text, merged metadata, and Mem0 update result.

Routine updates only need the memory id and corrected text. Add `--metadata
reason=...` only when it helps human audit.

`update` re-judges the text it just wrote (a self-checks-only `stale_check`,
returned as `stale_check_event`). Suspicion pairs are keyed on the judged
text's hash, so an update expires every flag standing against that memory; the
re-check exists so a half-finished fix cannot close its own flag and escape
review. A rewrite that did not actually resolve the defect gets flagged again.

Use destructive wipes only when explicitly requested:

```bash
memline delete --all --force --user-id workspace
```

Deletes append live audit rows too. A single-memory delete records the existing
memory snapshot before deletion; `delete --all --force` records the requested
scope and Mem0 result.

## Staleness / Supersession

Full semantics: `.agents/skills/local-memory/references/staleness-design.md`.
Invalidation is metadata-only (`superseded_by` list on the memory), reversible,
and audited; invalidated entries leave the default search pool but keep text,
history, and manifest rows. Every raw add also queues an advisory background
judge (`stale_check` event) — never a state change — which now produces three
suspicion kinds:

- `displacement`: an existing entry may be superseded by the new one;
- `necessity`: the new entry itself may not deserve long-term memory
  (verdicts: BORN_UNNECESSARY — activity narration / commit restatement /
  repo-readable fact; EXPIRING — progress tick / event-scoped coordination;
  DURABLE never opens a flag, and flags open only at confidence >= 0.8);
- `correctness`: the entry's claimed date/actor contradicts the CLI's
  authoritative metadata (TIMESTAMP_SUSPECT / ATTRIBUTION_SUSPECT), or its
  narrative is non-English while the store embeds English-only
  (LANGUAGE_SUSPECT — only technical identifiers may keep non-English chars);
- `ttl_expiry`: a TTL deadline fired — the entry left the pool and awaits
  review: `stale confirm` accepts the expiry, `stale ttl` renews it.

```bash
memline add "newer fact" --supersedes <old_id>[,<old_id2>]  # invalidate at write time
memline invalidate <memory_id> --by <new_id> [--reason "..."]
memline revive <memory_id>                                  # undo an invalidation
memline ttl <memory_id> [--days 7] [--clear]               # schedule/cancel reversible expiry
memline search "query" --include-superseded                 # history digs (includes expired)
memline get <memory_id> --resolve-head                      # follow the supersession chain
memline review [--session <id>] [--wait]                    # handoff: writes + raised suspicions
memline stale list [--session <id>]
memline stale confirm <pair_id>
memline stale dismiss <pair_id>
memline stale protect <memory_id> --kind displacement --days 30 --reason "..."
memline stale protected list [--include-expired]
memline stale unprotect <memory_id>
memline stale merge <pair_id> "<consolidated text>"
memline stale ttl <pair_id> [--days 7]                     # snapshot still alive: expire later
```

TTL semantics: `ttl` schedules a reversible pool-exit deadline (default 7
days). The search filter honors it lazily, so the entry leaves default search
at the deadline even if the daemon is down; the daemon materializes expiries
in the background and opens a `ttl_expiry` review flag for each, listed in
`review` under `ttl_expired` (any session may dispose those: confirm accepts,
`stale ttl` renews and re-enters the pool). `ttl --clear` re-enters the pool
at any time; setting a new deadline also acts as renewal.

`stale merge` is for pairs where the new entry ADDS detail rather than
replacing the old answer: the newer memory is updated to the consolidated
text (both entries' still-valid facts), the older memory is invalidated
pointing at it, and the pair closes as `merged`. Same authority rule as
`confirm`.

Rules that matter in practice:

- `invalidate` refuses cycles and double-invalidation; `--supersedes` is only
  valid on the raw (non-infer) add path.
- Suspicion pairs are keyed `(new_id, old_id, hash(old_text))`: updating a
  memory's text automatically expires prior judgments about it; dismissals are
  pair-level and permanent for that exact text-versioned pair. A different new
  entry may still raise a new suspicion against the same memory.
- `stale protect` is a bounded displacement-only noise-control valve, not a
  retention or correctness override. Default duration is 30 days, maximum 90;
  a 1-500 character reason and setter identity/session are stored. Safety,
  correctness, and necessity always run. A text update or invalidation clears
  protection. The core setter requires the latest three opening displacement
  suspicions for the current text version to all be dismissed with distinct
  `new_id` values. Any newer non-dismissed disposition interrupts the run;
  there is no force bypass.
- `stale confirm` from a non-interactive session is allowed only for pairs
  raised by that session's own writes; cross-session backlog needs an
  interactive session. The same authority gate applies to `stale dismiss`;
  cross-session disposition prompts for confirmation (default No) in an
  interactive session, or requires explicit `--force` non-interactively.
- `stale confirm` on a necessity flag expires the entry immediately
  (reversible via `ttl <memory_id> --clear`) — a self-suspicion has no
  superseder to invalidate by. Correctness flags are corrected via `update`
  (fix the date/actor, or re-express the entry in English; this auto-expires
  flags judged against the old text) and closed with `stale dismiss`.
- Session handoff (the "过期审查" the user requests before ending a session):
  run `review`, then dispose every `self_flags` item — born-unnecessary own
  entries may be hard-`delete`d; still-alive snapshots get `stale ttl`;
  correctness flags get `update`. The `suggested` field on each flag spells
  out the options.
- Deleting a memory closes open pairs that reference it. `delete` refuses to
  hard-delete a supersession-chain participant and downgrades to immediate
  reversible expiry instead (`downgraded_to_expiry` in the result).
- Search hits with open suspicions carry `suspected_stale: true` plus a
  `suspicions` list (kind, suspected_by, verdict, confidence, reason).

## Diagnostics

```bash
memline status
memline embed-test "hello"
memline history <memory_id>
```
