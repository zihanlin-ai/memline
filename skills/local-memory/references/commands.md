# Mem0 Local Command Reference

Use built-in help first:

```bash
mem0-local --help
mem0-local add --help
mem0-local search --help
```

## Core Commands

Routine agents should use only the simple forms below. The CLI handles writer
identity, session id, timestamps, schema fields, and JSON output automatically.

```bash
mem0-local status
mem0-local add "memory text"
mem0-local search "query"
mem0-local list
mem0-local get <memory_id>
mem0-local update <memory_id> "updated memory text"
mem0-local delete <memory_id>
```

When called from Codex/Claude contexts, output defaults to agent-readable JSON.
Use `--json` only when portability matters, and use `--output text` or
`--output table` for human-readable output.

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
mem0-local add "memory text"
```

Every successful live `add` appends one external audit row to
`.agent-memory/manifests/live-YYYY-MM.jsonl`. The row includes the raw input
content, infer mode, automatic metadata, scope, Mem0 result, memory ids/result
memories, timings, and payload hash. Agents do not need extra flags for this.

Override auto-detection only when it is missing or wrong:

```bash
mem0-local add "memory text" --metadata source=agent-memory-ledger --metadata session_id=manual-import
```

Advanced only: use `--no-infer` for exact audit entries when the user
explicitly asks for raw wording, or when normal inference repeatedly distorts the
fact. Do not use it for routine memory writes.

```bash
mem0-local add "exact ledger entry" --metadata source=agent-memory-ledger --no-infer
```

Historical imports may override event time:

```bash
mem0-local add "old memory text" --timestamp "2026-05-18T00:00:15+08:00" --no-infer
```

## Search

```bash
mem0-local search "proxy benchmark"
mem0-local search "proxy benchmark" --rerank
```

`search` returns `created_at`, `updated_at`, and metadata timestamps when present.

Keep `search` as pure semantic retrieval. Do not use it for agent/session/time
scoping; use `list --filter ...` for structured enumeration and audit queries.

Search is more reliable when the query includes English terms and exact
technical identifiers, because inferred memories are often stored in English.
For Chinese user questions, translate the core intent into English and keep
important literals unchanged:

```bash
mem0-local search "service fully ready proxy 7.246.46.187:7000 P 9000 D ports 9100 9101 9102 9103 /v1/models"
mem0-local search "ACS Bench provider endpoint 7.246.46.187:9000 pangu_ultra_moe"
mem0-local search "baseline_all_features VLLM_TORCH_PROFILER_DIR VLLM_TORCH_PROFILER_RECORD_SHAPES"
```

Use `--rerank` only for deliberate experiments; in the current local setup it
can be much slower than base search.

## List by Scope and Time Range

Use `list`, not semantic `search`, when the user asks to enumerate all memories
from a scope or date/time range. Use `--filter` for agent/session/time filters.

Use top-level fields for ordinary writer/session scope:

```bash
mem0-local list --filter agent_id=codex --page-size 100
mem0-local list --filter run_id=019eb447-4302-7f32-9eeb-66bfbe5f7d51 --page-size 100
mem0-local list --filter run_id=legacy-codex --page-size 100
mem0-local list --filter run_id=ledger-2026-05 --page-size 100
mem0-local list \
  --filter '{"agent_id":"codex","run_id":"019eb447-4302-7f32-9eeb-66bfbe5f7d51"}' \
  --page-size 100
```

## Optional Daemon

If repeated memory commands are slow, start the optional local daemon:

```bash
mem0-local daemon start
mem0-local daemon status
mem0-local daemon stop
```

The daemon is a local Unix-socket process under the workspace memory store. It
keeps the Mem0/FastEmbed/ONNX client warm, so `get`, `list`, base `search`, and
raw writes avoid per-command cold start. Commands fall back to the direct path
when the daemon is not running. To deliberately use the direct path for
debugging, stop the daemon first and then run:

```bash
MEM0_LOCAL_NO_DAEMON=1 mem0-local get <memory_id>
```

Use metadata fields such as `source` or `session_id` for import/audit queries
or when the metadata-level value itself is the intended filter:

```bash
mem0-local list --filter source=agent-memory-ledger --page-size 100
mem0-local list --filter session_id=019eb447-4302-7f32-9eeb-66bfbe5f7d51 --page-size 100
mem0-local list --filter writer_agent_id=ledger-importer --page-size 100
mem0-local list --filter origin=ledger_import --page-size 100
mem0-local list --filter memory_schema_version=2 --page-size 100
```

All existing memories were backfilled to schema v2. The git-managed audit
manifest lives under `.agent-memory/manifests/metadata-backfill-*.jsonl`.

Use JSON range filters with ISO-8601 timestamps for time ranges.

Use `created_at` for the memory's event timestamp. For ordinary `add`, this is
usually the write time; for historical imports it may be the original ledger
time.

```bash
mem0-local list \
  --filter '{"created_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

Use `ingested_at` for the actual time the memory entered Mem0/Qdrant.

```bash
mem0-local list \
  --filter '{"ingested_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

Combine scope filters with time filters by putting them in the same JSON object:

```bash
mem0-local list \
  --filter '{"agent_id":"claude","ingested_at":{"gte":"2026-06-24T00:00:00+08:00","lt":"2026-06-25T00:00:00+08:00"}}' \
  --page-size 100
```

## Get, List, Update, Delete

```bash
mem0-local get <memory_id>
mem0-local list --filter agent_id=codex
mem0-local list --filter run_id=<session_id>
mem0-local update <memory_id> "new text" --metadata reason=correction
mem0-local delete <memory_id>
```

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

Use destructive wipes only when explicitly requested:

```bash
mem0-local delete --all --force --user-id workspace
```

Deletes append live audit rows too. A single-memory delete records the existing
memory snapshot before deletion; `delete --all --force` records the requested
scope and Mem0 result.

## Diagnostics

```bash
mem0-local status
mem0-local embed-test "hello"
mem0-local history <memory_id>
```
