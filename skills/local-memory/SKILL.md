---
name: local-memory
description: Operate the workspace memory system through the `memline` CLI — both the local memory store (search, add, get, list, update, delete, audited import, end-of-session handoff review / 过期审查) and the LLM Wiki built on top of it at .agent-memory/wiki (compile 跑一次 compile / 出建议, record verdicts, draft accepted topics, audit citations, publish approved articles, maintain routing and provenance, or explain how the wiki is organized). Trigger for any of those, or to troubleshoot the Mem0/Qdrant-backed store.
---

# Local Memory and the LLM Wiki

One CLI, two layers. `memline` owns the memory store — the always-on layer
every session writes to and searches. `memline wiki` distills that store (plus
designated documents) into the workspace wiki at `.agent-memory/wiki/` —
a manual, human-gated layer that only moves when the user asks.

This file carries what every session needs: the memory core and the wiki's
hard gates. Everything procedural loads on demand from `references/` — do not
read a wiki reference until the user asks for that stage of the pipeline.

## Scope and Ownership

The source of truth for this skill is the `memline` repository, at
`skills/local-memory/`. The workspace exposes it through a symlink —
`.agents/skills/local-memory` -> `.agent-memory/projects/memline/skills/local-memory`,
and `.claude/skills` is itself a symlink to `.agents/skills` — so every path
resolves to the same files and they cannot drift. `memline` is a submodule of
the workspace: edits are committed and pushed in that repository, and the
workspace commit only moves the submodule pointer. A workspace clone made
without `--recurse-submodules` leaves the symlink dangling and the skill absent.

Policy still travels with the skill rather than with the CLI code. Workspace
policy, handoff procedures, and the wiki judgement — what counts as one
article, what a review must catch, what may be published — live in this file
and its `references/`, so the CLI's behavior and the agent's obligations stay
separable even though they now ship from one repository.

## Memory: core usage

Call `memline` directly; it is installed on PATH.

```bash
memline start                                     # manual recent-window recall / hook fallback
memline add "accurate memory text"
memline add "required non-Latin text" --force    # language-gate override; never bypasses the raw length cap
memline add "newer fact" --supersedes <old_id>   # when this write knowingly replaces an old entry
memline search "query"                            # retired entries excluded by default
memline search "query" --include-superseded       # history digs: include retired entries
memline get <memory_id>
memline update <memory_id> "updated memory text"
memline delete <memory_id> --force   # destructive ops confirm on a TTY; non-interactive agents must pass --force (preview with --dry-run)
memline stale protect <memory_id> --reason "..." [--days 30]  # bounded displacement-only suppression
memline entity list --contains "<text>"
```

In Codex/Claude contexts, output defaults to agent-readable JSON. Use `--json`
explicitly only for portability, or `--output text` for human-readable output.

## Memory: core rules

### Stack behavior

- The stack is self-healing: any `memline` command auto-starts the daemon when it is down (~15-30s once after a reboot; later calls are fast), and the daemon in turn starts the local qdrant server if needed. No manual `daemon status`/`daemon start` is required at session start. Opt out with `MEMLINE_NO_AUTOSTART=1`; `MEMLINE_NO_DAEMON=1` still forces the direct path.
- Auto-start is namespace-guarded: from an isolated sandbox context (host qdrant unreachable) the CLI refuses to spawn the daemon or a duplicate qdrant and prints a hint to run `memline daemon start` from a normal shell instead. If memory commands report qdrant connection errors in such a context, that is the sandbox, not the store.
- Do not manually pass source, agent id, session id, timestamps, schema fields, or output formatting for routine use. `add`, `search`, and `update` automatically include timestamps, writer/session identity, schema metadata, and agent-readable JSON output in agent contexts. `update` preserves original writer/session scope and records the current updater identity — supply only the corrected text and optional human-meaningful reason metadata.
- `add`, `update`, and `delete` automatically append external audit rows to `.agent-memory/manifests/live-YYYY-MM.jsonl` by month. Agents do not need to manage this manifest; it exists for human audit.

### Write discipline

- Write small, frequent, single-fact entries — one `add` per atomic fact, called often — not big multi-fact paragraphs. Raw writes enforce a hard length cap (default 600 chars): an over-cap `add` errors before touching the store — split it into multiple single-fact adds (or use `--infer` extraction for genuinely long conversational input). `update` may keep or shrink an over-cap legacy entry but cannot grow it past the cap.
- Plain-text `add` stores the exact text verbatim by default (raw mode): synchronous, ~1s, returns the memory id, exact-hash dedup, near-duplicate annotation, entity-linked. Because no LLM normalizes the text, the writer must produce entries that are atomic, self-contained (no pronouns or session-relative references), dated, and rich in exact identifiers.
- Raw adds deterministically reject non-Latin letters before any store or audit mutation. Rewrite the narration in English; use `--force` only when non-Latin text must be preserved. This override applies only to the language gate and never bypasses the raw length cap. Extraction inputs (`--infer` / `--messages` / default `--file`) are not raw writes and remain governed by the post-write correctness judge.
- Raw storage is the default because agent-authored entries are already atomic facts and verbatim fidelity beats LLM rewriting for paths, hosts, commands, and dates (policy updated 2026-07-16; the older infer-by-default rule is superseded).
- Raw adds hash-dedup exact re-fires (event `NONE` with `duplicate_of`) and annotate semantic near-duplicates with `near_duplicate_of`/`near_duplicate_score` (cosine >= 0.95) without skipping the store — review the hint and `delete --force` the redundant copy if it truly duplicates.

### Async extraction (`--infer` / `--messages` / `--file`)

- LLM extraction runs for `--messages`/`--file` input (conversations genuinely need it) or when `--infer` is passed explicitly. Extraction adds are queued asynchronously: they return `{event_id, status: queued}` in about a second; do not wait or poll routinely. Use `--infer --wait` to confirm inline. Check progress with `event status <event_id>`.
- After extraction completes, inspect the stored memories for accuracy; correct distortions promptly with `update` or `delete --force` + re-add.
- Async extraction failures are retried up to 3 times, then marked failed and announced as a stderr warning banner on later hygiene-command invocations (`review`/`stale`/`event`/`status`; routine `add`/`search`/`start` stay quiet). On seeing the banner: `event list --status failed`, then `event retry <event_id>` (requeues) or `event ack <id>|--all` (dismisses).

### Retrieval

- Supported harness integrations inject a recent window when a new context starts and restore this session's writes after context compaction. Do not read `.agent-memory/MEMORY.md` or call `memline start` as routine session bootstrap; resume, fork, and clear preserve or reset context without another recall injection.
- Use `memline search` whenever task-specific or older context might help. Automatic recall is deliberately limited by time, count, and character budget, so it does not replace semantic retrieval.
- Use `memline start` only to inspect or widen the recent window manually, or as a fallback when the harness recall hook is unavailable or demonstrably failed.
- Keep `search` as pure semantic retrieval: pass a query, optionally `--top-k` or `--rerank`, and do not use it for agent/session/time scoping. Default retrieval is hybrid (vector + BM25); `--keyword` switches to pure BM25 term matching (exact identifiers, paths, error strings). The local `--threshold` default is 0.1 (official CLI: 0.3) on purpose — local hybrid scores are distributed lower; do not "align" it to 0.3.
- Prefer English search terms, exact paths, commands, ports, model names, and environment variables. If the user asks in Chinese, keep the Chinese intent but add the likely English entities/keywords.
- Use `list --filter ...` only when the user asks to enumerate/audit memories by structured fields such as time range, writer, session, source, or import batch. Field details: [commands.md](references/commands.md).

### Lifecycle hygiene

- Lifecycle hygiene is deliberately invisible during task execution: background judges quietly mark entries for later; `suspected_stale`/`suspicions` fields on search hits are advisory; stderr suspicion banners require NO action mid-task. The only lifecycle action that belongs inside a task is `add "..." --supersedes <old_id>` when a write knowingly replaces an evolving old fact.
- Everything else happens only when the user asks for a session handoff (e.g. "做一次过期审查"): follow [handoff-review.md](references/handoff-review.md). The acceptance criterion is `memline review --wait --check` returning `verdict: pass` (exit 0): everything this session wrote is handled. Other sessions' backlog is excluded by design and never blocks a pass.
- The session-handoff banner is the one stderr banner addressed to the current agent: once a session's live adds exceed the threshold (default 200), every `memline` command prints "this session has accumulated N memory add(s)". On seeing it, tell the user and suggest a handoff; run the review only after they agree.
- `stale dismiss` asserts the judge was WRONG, and is permanent for that exact text version — it is not an acknowledgement. A `LANGUAGE_SUSPECT` flag is about the language the entry is written in, not whether its facts are right, so "the content is correct" is never grounds to dismiss it: rewrite with `update` instead, or leave the flag open. Every flag in `review` carries a `suggested.dismiss_only_if` naming the single condition under which dismissing is correct.
- Disposition authority is CLI-enforced for `stale confirm` / `dismiss` / `merge` / `ttl`: same-session pairs are allowed; cross-session pairs require a default-No interactive confirmation or explicit user-approved `--force` (`ttl_expiry` is exempt — any session may dispose those).

### Safety rails

- `delete` and `entity delete` are guarded: they prompt on a TTY and refuse non-interactively without `--force`; `--dry-run` previews. Do not use `delete --all --force` unless the user explicitly requests a scoped wipe.
- Never print or read the local secret env file.
- Memory entries must never contain plaintext credentials — reference the secret's location instead. If an existing entry holds one, redact it in place with `update <id> "<same text, secret replaced by a pointer>"` (preserves id, links, history), then verify `search "<secret literal>" --include-superseded` returns nothing.

## The wiki layer

The wiki distills the memory store and designated documents into English
articles of vLLM-Docs/Blog quality at `.agent-memory/wiki/`. Only
`content/` there is authoritative; profiles, suggestions, drafts and rejected
material must never be used as authoritative context by any agent.

The pipeline is split between four actors, and getting the split wrong is the
main way to damage it:

| Actor | Does | Never does |
|---|---|---|
| **Programs** (`memline wiki ...`) | batching, sanitizing, external calls, citation joins, report validation, computed listings/relations | judge support or what is worth writing |
| **External model** | profile a batch; draft a topic; independently audit a review bundle | decide topics, repair unresolved refs, publish |
| **Agent** (you) | associate profiles, propose suggestions, adjudicate findings, review drafts, publish | invent verdicts, trust model self-review, publish unapproved |
| **User** | every accept / reject / defer, and final approval of every article | — |

Hard gates, always in force:

- **Never auto-publish, never draft unrequested.** The gates are: user accepts
  a topic → user asks for a draft → user approves the draft → publish.
- **Sanitize before anything leaves this machine.** `memline wiki bundle` and
  `wiki profile` do it; never hand-assemble a payload that skips them.
- **Blog is frozen once published** (append errata, never rewrite); **Docs are
  living**. Formal body text is English.
- **`content/docs/.nav.yml` is hand-written, never generated** — it carries
  entry points and reading order, judgement no generator has.

Health check at any time: `memline wiki check-pages --strict` — provenance,
links, metadata, and routing drift in one read-only report.

## Progressive references

Memory layer:

- Full command list, time-range listing, examples: [commands.md](references/commands.md).
- Paths, PATH/symlink details, store layout, Qdrant lock behavior, rollback checks: [troubleshooting.md](references/troubleshooting.md).
- Historical Markdown ledger migration and import audit: [imports.md](references/imports.md).
- End-of-session handoff workflow — load only at handoff time, never mid-task: [handoff-review.md](references/handoff-review.md).
- Staleness/supersession design, review false-positive rubric, lineage retracing: [staleness-design.md](references/staleness-design.md).

Wiki layer — load exactly the stage the user asked for:

- User asks to **compile** (跑一次 compile / 出建议), process suggestions, or record verdicts: [wiki-compile.md](references/wiki-compile.md).
- User asks to **draft** an accepted topic, or audit/review a draft's citations or quality: [wiki-drafting.md](references/wiki-drafting.md).
- User approves **publishing**, or asks about corpus maintenance, routing (`.nav.yml`), listings, staleness flags, or how the wiki is organized: [wiki-publishing.md](references/wiki-publishing.md).
