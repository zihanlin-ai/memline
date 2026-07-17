# Staleness & Supersession Design for mem0-local

Status: steps 1-4 IMPLEMENTED 2026-07-17 on branch
`feature/staleness-supersession` of `.agent-memory/projects/mem0-local`
(4 commits, e07eeab..acc4192); the running daemon serves this code. Step 5
(full-store backlog pass) not started. SKILL.md intentionally not yet
updated — skill exposure of the new verbs awaits explicit user approval.
Judge eval gate: 24 labeled real pairs, accuracy 87.5%, SUPERSEDED
precision 90% / recall 81.8% (`tools/stale_judge_eval.py`).
This file is the behavioral spec; update it if the design changes.

## 1. Problem & Requirements

The store holds ~5.6k verbatim entries growing ~160/day (2026-07). Hybrid
retrieval is accurate for identifier-anchored queries (measured 5/6 rank-1),
but evolving-state topics accumulate time slices: one state query returned 10
versions of the same fact spanning 5 weeks with no currency signal. The store
is append-only since the 2026-07-16 raw-add policy; nothing ever leaves the
retrieval pool.

Hard requirements:

1. **Verbatim fidelity** — stored text is never rewritten by any LLM.
2. **No information loss** — no text is silently deleted or overwritten;
   "invalidation" only removes an entry from the default retrieval pool.
   Everything remains in the vector store, history.db, and manifests, and is
   reversible.
3. **Accurate recall** — current facts must win; superseded versions must not
   compete in default search.
4. **Zero in-session burden** — agents keep using `add`/`search` naturally.
   The only human-in-the-loop moment is the existing handoff habit (agent
   reviews its own writes at session end).
5. **LLM is advisory only** — it proposes suspicions; the calling agent (or
   human) makes every state-changing decision. Exception: the writer agent may
   declare supersession itself at write time (it has the most context).
6. **Keep the stack** — mem0-local + Qdrant + manifests + event queue. No
   migration. Default search latency must stay ~0.6s (no LLM in the read
   path).

Summary: **background LLM proposes, handoff agent disposes, banner catches
leftovers; evidence is append-only, state is reversible, text is immutable.**

## 2. Data Model

### 2.1 Memory state (one derived field per memory)

```
active       default; in the retrieval pool
suspected    derived flag: has >=1 open suspicion pair; still in pool,
             flagged in search results
invalidated  superseded_by is set; out of the default pool; text preserved
```

New payload fields on memories:

| field | type | meaning |
|---|---|---|
| `superseded_by` | list of memory ids (usually length 1) | set on invalidation; absent/empty = active |
| `superseded_at` | ISO ts | when invalidated |
| `superseded_reason` | string | short human/agent-readable reason |
| `stale_check_pin` | bool | memory-level immunity: never judge this entry |

### 2.3 Supersession topology: DAG stored as adjacency lists, not a graph DB

The supersession relation is logically a DAG; the typical shape is a short
chain (A←B←C). Storage is just the per-memory `superseded_by` pointer list:

- **1→1 replacement** (the >90% case): single-element list, forms a chain.
- **N-merge-1** (duplicates consolidated into one entry): each old entry
  points to the same successor — still one outgoing edge per node; the "many"
  is the successor's in-degree. Single pointers suffice.
- **1-split-N** (one coarse entry split into finer facts): the only case
  needing an out-degree > 1, hence the list type. Rare but real.

Guards: `invalidate` walks the ancestor chain and refuses to create a cycle.
`resolve-head` follows pointers until active entries and returns the full set
of heads (usually one).

Why not a graph store: supersession edges are sparse, local, and shallow
(few-hop traversal); topical relatedness is already covered by the existing
entity graph (entity → linked_memory_ids), a semantically different relation
kept separate. A Qdrant payload field + the SQLite evidence table matches the
problem size; the pointers can be exported losslessly into a graph engine
later if multi-hop queries ever become real.

### 2.2 Suspicion pairs (append-only evidence, separate from state)

One record per judged pair:

```
pair_key   = (new_id, old_id, sha256(old_text))   # text-versioned
verdict    = SUPERSEDED | DUPLICATE | KEPT        # judge output
confidence = 0.0 - 1.0
reason     = one sentence naming the shared slot (shown to the reviewer)
judged_at, judge_model
disposition = open | confirmed | dismissed | obsoleted | expired
disposed_by, disposed_at
```

- Only `verdict in (SUPERSEDED, DUPLICATE) and confidence >= 0.6` creates an
  *open* suspicion. Lower-confidence results are still cached (as judged-KEPT)
  so pairs are never re-judged.
- The `old_text` hash in the key makes verdicts version-scoped: updating a
  memory's text automatically invalidates prior judgments about it (old pairs
  no longer match; the pair becomes judgeable again against the new text).
- Storage: a small SQLite table next to history.db (`stale.db` or a new table
  in history.db). Not in Qdrant.

## 3. Pipelines

### 3.1 Write path (unchanged + one hook)

- `add` stays raw, synchronous (~1s), verbatim, hash-dedup, entity-linked.
- After a successful add, enqueue a `stale_check` event on the existing async
  event queue (same retry/failed-banner machinery as `--infer` adds).
- Optional fast path for confident writers: `add "..." --supersedes <id[,id]>`
  invalidates the listed entries immediately (no LLM involved), audited to
  manifests.

### 3.2 Background judge (advisory only)

- Daemon consumes `stale_check` events. For each new entry: candidate set =
  top-k (default 10) similar **active** entries (reuse the embedding computed
  at add time), minus pinned entries, minus already-cached pairs.
- One batched LLM call per new entry (new vs up-to-k candidates).
- **Decided 2026-07-17:** the judge reuses the existing `[llm]` preset
  (`@preset/work`). The async queue absorbs its latency (~160 calls/day is
  well within background throughput); per-call cost is accepted. If cost or
  queue lag ever becomes a problem, adding a separate `[stale_llm]` config
  section is the designated escape hatch — no redesign needed.
- Results are written as suspicion-pair records. No state change ever happens
  here.

### 3.3 Handoff review (the disposition point)

`mem0-local review --session [<id>]` (session auto-detected like add/search):

1. List this session's writes (structures the existing self-review habit).
2. Show precomputed open suspicion pairs involving those writes, grouped by
   target, sorted by (independent-suspicion count, confidence). Judge any
   still-unjudged remainder on demand (normally only the last few minutes of
   writes).
3. Agent disposes each pair:
   - `confirm` → invalidate target (`superseded_by` = new id)
   - `update <id> "..."` → correct the old entry instead (clears its open
     suspicions; pair cache resets via text hash)
   - `dismiss` → pair-level permanent closure
   - `dismiss --pin` → also set `stale_check_pin` on the target (for durable
     method notes that attract repeated false suspicion)

**Disposition authority (decided 2026-07-17):** a non-interactive session may
only dispose pairs whose *new* side was written by that same session (its own
writes triggered the suspicion). Cross-session backlog pairs are reserved for
interactive sessions with the user present. Enforced by `review --session`
scoping; `stale list` shows backlog pairs read-only to unattended agents.
Authority may widen later once judge-quality data exists.

### 3.4 Safety net

Open suspicions from crashed/unreviewed sessions persist. Reuse the stderr
warning-banner mechanism: any later CLI invocation shows "N open staleness
suspicions — run `mem0-local stale list`". Session-start rule (MEMORY.md)
gains one line: glance at `stale list` alongside the last-1-day listing.

## 4. Retrieval Changes

- `search` default: exclude `invalidated`; annotate `suspected` hits with
  `suspected_stale: true`, `suspected_by`, and the reason — stale hits become
  flagged instead of silent poison, before any disposition happens.
- `--include-superseded` returns everything (for history digs).
- `get --resolve-head <id>`: follow `superseded_by` pointers to the current
  active head(s); returns the full set when a split produced more than one.
- No retriever changes otherwise. No LLM rerank by default (25s measured;
  manual `--rerank` stays for deep digs). English-keyword query discipline
  stays (measured ~0.1 score penalty and worse ranking for Chinese queries).

### 4.1 Agent-facing interface delta (what eventually gets exposed in skills)

In-session agents learn nothing new: `add`/`search` calls are unchanged;
search results passively gain the suspected-stale annotation and stop
returning invalidated entries. Optional extras: `add --supersedes`,
`--include-superseded`, `get --resolve-head`. The one genuinely new required
command is `review --session` at handoff (with `confirm` / `dismiss [--pin]`
/ `update` dispositions) plus `stale list` as the banner follow-up.
Repair verbs (`invalidate`, `revive`) are rare and mostly human-driven. The
background judge is invisible to all callers. None of this is exposed in
SKILL.md until implemented and approved (see Status header).

## 5. Judge Prompt Design

Core concept: **slot displacement, not falsehood.** Entries are dated
snapshots; "On 6-26 probe achieved X" is true forever. The judged question is
whether the new entry displaces the old as the *current answer to the same
slot* (subject × configuration × metric/aspect).

System prompt skeleton (English; fixed text to exploit prompt caching;
per-call user message carries only the new entry + candidates as JSON):

```text
You are a staleness judge for an engineering memory store. Entries are
dated, verbatim snapshots written by agents: measurements, configs,
service states, decisions, and method notes.

Given ONE new entry and K existing entries, decide for EACH existing
entry whether the new entry DISPLACES it as the current answer.

Core concept — the SLOT: every entry answers an implicit question
defined by (subject × configuration × metric/aspect). An entry is
SUPERSEDED only when the new entry answers the SAME slot with newer
information. A dated snapshot is never "false"; the only question is
whether it is still the latest answer for its slot.

Verdicts:
- SUPERSEDED: same slot, new entry is the newer answer
  (state replaced, measurement re-run under the SAME config,
  decision revised, path/URL/owner changed).
- DUPLICATE: same slot AND same information; the new entry adds
  nothing (merge candidate).
- KEPT: everything else. Default when uncertain.

Never SUPERSEDED:
- Different configuration, host, dataset, model, or parameter values
  → different slot, results coexist.
- Method/playbook notes vs. instance facts: a new measurement never
  displaces a "how to" note, and vice versa.
- New entry adds detail or a follow-up event about the same subject
  without replacing the old answer.
- Old entry is a root-cause/conclusion; new entry is merely a later
  data point consistent with it.

Rules:
- Judge ONLY from the texts given. Reference only the provided ids.
- confidence: your probability that a reviewer will confirm.
- reason: ONE sentence a reviewer reads to decide; name the shared
  slot explicitly.

Output JSON only:
{"judgments":[{"id":"<id>","verdict":"SUPERSEDED|DUPLICATE|KEPT",
"confidence":0.0,"reason":"..."}]}
```

Few-shot examples must come from this store's real domain, e.g.:

- pd-cap probes at different TPS/configs → KEPT (different slot);
- dashboard URL entry vs newer URL entry → SUPERSEDED (same slot);
- "CURRENT as of ..." methodology note vs a new measurement → KEPT
  (method vs instance).

**Evaluation set before wiring anything:** 30–50 hand-labeled pairs sampled
from the store (the 2026-07-17 recall test already produced several). Rerun on
every prompt/model change. Gate: precision on SUPERSEDED matters more than
recall (false suspicions spend the reviewer's handoff budget; misses just fall
back to the status quo).

## 6. Edge-Case Rules (state machine)

1. **Multiple suspicions on one target** — separate evidence records
   accumulate; the target's flag is just "has open suspicions". Review groups
   by target; independent-suspicion count is a priority signal.
2. **Exact pair re-fired** — absorbed by the pair cache; never re-judged.
3. **Dismissed, later re-suspected by a different new entry** — a new pair is
   legitimately opened (new evidence). Prior dismissal only closes its own
   pair. Repeated false positives on one entry → `dismiss --pin`.
4. **Supersession chains (A←B←C)** — invalidating B sets `B.superseded_by=C`;
   A's pointer to B is historical truth and is never rewritten. Resolve the
   head by following pointers. Judges only see **active** candidates, so no
   redundant suspicions against already-invalidated entries.
5. **Target invalidated before disposition** — open suspicions on it
   auto-close as `obsoleted`.
6. **Superseder itself deleted/invalidated** — never auto-revive its victims.
   The review flow surfaces "X superseded Y — revive Y?"; manual
   `mem0-local revive <id>` clears `superseded_by` (audited).
7. **Target text updated** — text-hash pair keys make prior judgments (open
   suspicions and dismissals) expire automatically; the entry is judgeable
   again against its new text.
8. **Concurrency** — evidence is append-only (conflict-free); state fields go
   through the existing CLI lock; every mutation is replayable from manifests.
9. **Cycles** — `invalidate` refuses any edge that would make the new entry an
   ancestor of itself in the supersession DAG.

Invariants: no automatic path changes memory state; no path ever deletes or
rewrites text; worst-case failure mode is "a few extra pairs waiting for
review".

## 7. Non-Goals

- No migration to Graphiti/Letta/Cognee (triplet extraction misfits dense
  parameterized measurement facts; LLM-in-write-path conflicts with verbatim
  requirement). Graphiti's invalidation semantics are what §2 implements as
  metadata.
- No LLM reranker by default; no retriever/embedder replacement.
- No TTL-style forward expiry: invalidation is only ever triggered backward,
  by the arrival of a superseding fact (plus the advisory judge). A future
  recency-decay downrank for `kind=state` entries is orthogonal and out of
  scope here.

## 8. Implementation Plan

| step | deliverable | verification |
|---|---|---|
| 0 | this document reviewed | user sign-off |
| 1 | `superseded_by` fields, `invalidate`/`revive` verbs, `--supersedes`, search filter + suspected flagging, `--include-superseded`, `get --resolve-head` | hand-mark a few known-stale entries; rerun the recall regression queries |
| 2 | judge prompt + few-shots + 30–50 pair labeled eval set + eval runner | precision/recall gate passes with the chosen cheap model |
| 3 | `stale_check` event type, pair cache, daemon consumer, judge-model config | one day of live adds; inspect suspicion quality |
| 4 | `review --session`, `stale list`, disposition verbs, warning banner, MEMORY.md/skill rule updates | a real handoff end-to-end |
| 5 | **full-store** backlog pass over all ~5.6k entries incl. the 2,526 ledger imports (decided 2026-07-17): batched judge runs in background; dispositions consumed in chunks during dedicated interactive sessions (per the disposition-authority rule, backlog confirm requires the user present) | rerun the recall regression; the 10-time-slice state query should return only current + flagged entries |

Implementation location (decided 2026-07-17): directly in the vendored
mem0-local package; bump the version suffix to `+workspace.2`. Future
upstream syncs must carry these patches (note it in the vendoring notes).

Success criterion: the recall regression script (to be stored in
`.agent-memory/utils/`) shows identifier queries unchanged (rank-1 kept) and
state queries returning the current version first with superseded versions
absent from default search.
