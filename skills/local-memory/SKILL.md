---
name: local-memory
description: Use the `mem0-local` CLI for workspace-local memory search, add, get, list, update, delete, and audited historical memory import. Trigger when the user asks to search, add, migrate, audit, troubleshoot, or inspect local memory backed by the workspace Mem0/Qdrant store.
---

# Local Memory

## Core Usage

Call `mem0-local` directly; it is installed on PATH.

```bash
mem0-local --help
mem0-local <command> --help

mem0-local add "accurate memory text"
mem0-local search "query"
mem0-local get <memory_id>
mem0-local update <memory_id> "updated memory text"
mem0-local delete <memory_id>
```

In Codex/Claude contexts, output defaults to agent-readable JSON. Use `--json` explicitly only for portability, or use `--output text` / `--output table` for human-readable output. Do not add `--no-infer` for routine memory writes.

## Core Rules

- At session start or before multiple memory operations, run `mem0-local daemon status`. If it is not running and memory commands are expected to be repeated, start it with `mem0-local daemon start`; the daemon is optional, local, and not expected to auto-start at boot. If the current sandbox cannot connect to the daemon socket, do not keep retrying; stop the daemon or set `MEM0_LOCAL_NO_DAEMON=1` and use the direct CLI path.
- Write small, frequent, single-fact entries — not big multi-fact paragraphs. One `add` per atomic fact, called often, beats one long dense entry. This matches Mem0's one-fact-per-memory retrieval model and avoids the LLM-extraction backend truncating long entries (seen failures: `Error parsing extraction response: 'NoneType' object has no attribute 'strip'` / `Unterminated string`). If an `add` returns empty `results`, determine why: if it was deduplicated because the same memory already exists, no action is needed; if extraction failed or the input was too long/dense, split/rewrite into shorter atomic entries and add again.
- Use normal inferred `add` by default. After infer writes, inspect returned memories for accuracy; if Mem0 extracted an inaccurate, stale, distorted, or misleading memory, correct it promptly with `update`, `delete` + re-add, or a clarifying replacement memory. Do not use `--no-infer` unless the user explicitly asks for exact audit text or normal inference repeatedly distorts the fact.
- Do not manually pass source, agent id, session id, timestamps, schema fields, or output formatting for routine use. `add`, `search`, and `update` automatically include timestamps, writer/session identity, schema metadata, and agent-readable JSON output in agent contexts.
- In agent contexts, `add`, `search`, `list`, `get`, `update`, `delete`, and `status` return JSON by default unless `--output ...` is explicitly passed. Agents should not add formatting flags unless they need a non-default format.
- `search` and `get` return the stored timestamps.
- Keep `search` as pure semantic retrieval: pass a query, optionally `--top-k` or `--rerank`, and do not use it for agent/session/time scoping.
- Use `list --filter ...` only when the user asks to enumerate/audit memories by structured fields such as time range, writer, session, source, or import batch. See [commands.md](references/commands.md) for field details.
- To enumerate memories in a date/time range, use `list --filter` with a JSON range filter instead of semantic `search`; see [commands.md](references/commands.md) for `created_at` versus `ingested_at` examples.
- Prefer English search terms, exact paths, commands, ports, model names, and environment variables. If the user asks in Chinese, keep the Chinese intent but add the likely English entities/keywords.
- `update` preserves original writer/session scope and automatically records the current updater identity. Agents should only supply the corrected memory text and optional human-meaningful reason metadata.
- At session start, after reading `.agent-memory/MEMORY.md`, list memories ingested in the last 1 day with `since="$(python3 -c 'from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(days=1)).isoformat())')"; mem0-local list --filter "{\"ingested_at\":{\"gte\":\"$since\"}}" --page-size 100`; then use semantic `search` for task-specific recall.
- Never print or read the local secret env file.
- Do not use `delete --all --force` unless the user explicitly requests a scoped wipe.

## Progressive References

- For the full command list, time-range listing, and common examples, read [commands.md](references/commands.md).
- For real paths, PATH/symlink details, reusable package location, workspace config, store layout, Qdrant lock behavior, missing command issues, or rollback checks, read [troubleshooting.md](references/troubleshooting.md).
- For historical Markdown ledger migration policy, timestamp-source rules, and dry-run/import audit guidance, read [imports.md](references/imports.md).
