# Compiling the wiki: memories in, suggestions out

Manual trigger only. Six steps; do not skip the ordering. Load this file when
the user asks for a compile (跑一次 compile / 出建议) or to process
suggestions — never run any of it unrequested.

## Layout you will touch

```
suggestions/runs/run-NNN/        one compile run, immutable once complete
  plan.json                        batch plan
  profiles/<batch>.json            raw profiles — the record of what was said
  sources/<doc>.json               raw source-document profiles
  associations.json                your association judgement, with reasons
  suggestions.jsonl                the suggestions shown to the user
  run.md                           report header
suggestions/decisions.md         living ledger, one line per verdict
suggestions/accepted-topics.jsonl living queue of accepted topics
state/compile.json               cursor: last run, source hashes, boundary ids
```

## 1. Plan

```
memline wiki plan --out suggestions/runs/run-NNN/plan.json
```

A session is a task unit, so it is the profiling unit. The planner produces
four kinds and they are not interchangeable: `session`, `pack` (several small
sessions sharing a call but not a topic), `session-part` (a long session cut
up, parts must be rejoined later), `ledger` (bulk-imported history with no
session structure).

For an incremental run, read `state/compile.json` first and plan only
memories with `ingested_at` or `updated_at` at or after the cursor (`--since`),
deduped against `boundary_memory_ids`. Record `run_started_at` BEFORE reading
anything.

## 2. Profile

```
memline wiki profile suggestions/runs/run-NNN/plan.json \
  --out-dir suggestions/runs/run-NNN/profiles --concurrency 2
memline wiki profile --source-dir sources \
  --out-dir suggestions/runs/run-NNN/sources --concurrency 2
```

Keep concurrency at 2 or lower: the relay queues, and a queued request is one
whose first byte arrives too late to survive the path. The pass is resumable —
re-running skips finished batches — so a partial failure is re-run, not
restarted.

**`ledger` batches are not profiled by the external model.** They have no task
structure and need closer reading, so dispatch local subagents (roughly one per
two chunks) with the packaged prompt
`<memline-repo>/src/memline/prompts/wiki-profile-session.md` as their contract,
writing the same record shape into the same `profiles/` directory. Material the
endpoint refuses on moderation grounds also comes here — a refusal is recorded
in the profile, and that batch becomes subagent work.

## 3. Associate — this is your judgement, not the model's

Read every profile. Each gives you sessions, and each session gives you
`threads` (lines of work) with `continues` markers. Decide which threads are
one article.

- **Follow the work, not the calendar.** Threads chain when they share a
  subject and a causal line: the same bug hunted across four evenings, a
  bring-up that resumed after a fix landed elsewhere. Two efforts that merely
  ran the same week are two articles.
- **`continues: before|after|both` is a strong hint, not proof.** Verify by
  subject before joining; a long session's parts almost always rejoin.
- **Respect the Blog floor.** A Blog is never smaller than one work session;
  the normal case spans several. One experiment, one crash, one fix inside a
  longer effort is a *section*, not an article. Docs pages have no such floor.
  The floor is measured against *work sessions only*: a `ledger` chunk is a
  fixed-size slice of imported history with unrelated work interleaved, so
  "all the threads in that chunk" is not a unit of work — ledger-derived
  topics are judged on whether the story is complete, not on chunk coverage.
- **Docs associate differently:** by *page*, not by story. The same procedure
  described in five sessions is one page, and one session may contribute to
  several pages.
- **Memories and source documents are equal material.** Neither is inherently
  Blog or Docs material: associate by subject, never by where the material
  came from. Material is many-to-many with content — the same section may
  serve a Blog and a Docs page at once.
- **Do not merge across a retraction.** When later work overturned earlier
  work, that is one article with an arc — say so in the suggestion — not two
  articles and not a silent preference for the newer claim.
- Write `associations.json` recording, per proposed topic: the member threads,
  the reason they belong together, and anything you deliberately kept apart.
  A later reader must be able to challenge your grouping.

Consult `decisions.md` before proposing: never re-emit a rejected suggestion
(identity is `type` + `topic_key` + evidence set), and pending suggestions from
an earlier run are still pending — do not propose them again.

## 4. Propose

Write `suggestions.jsonl`, one JSON object per line:

```json
{"id": "run-NNN-S001", "run": N, "type": "blog|docs-new|docs-update|erratum|maintenance|conflict-review",
 "topic_key": "stable-kebab-case", "title": "...",
 "value": "why this is worth an article (1-3 sentences)",
 "member_threads": [{"batch_id": "b012", "session_id": "...", "thread_key": "..."}],
 "evidence": [{"ref": "mem:<id>|sources/<path>#<heading>", "sha256": "<hash>"}],
 "evidence_gaps": "objective missing pieces — never a readiness verdict",
 "conflicts": "...", "sensitive": "...",
 "proposed_location": "content/docs/<chapter>/<slug>.md | content/blog/...",
 "affected_pages": "..."}
```

Output **all** suggestions, uncapped and unranked. Readiness is the user's
call: state gaps as facts and never say a topic is or is not ready. Conflicts
between materials get flagged in the suggestion, never silently resolved —
human-confirmed facts win.

`memline wiki suggest` checks the published pages itself and appends one
suggestion per affected page — `maintenance` for a living Docs page, `erratum`
for a frozen Blog one. Do not skip it and do not run the check separately as a
substitute: an incremental plan iterates the memories that exist, so a
*deleted* one leaves nothing for it to notice, and the page's own frontmatter
is the only surviving record that anything ever depended on it.
`--skip-page-check` exists for offline use and makes that dependency
permanently invisible.

Read `page_check` in the command's report: `flag_count` there and no
maintenance suggestions in the output means something is wrong with the run,
not that the pages are healthy.

## What compile may write into content/

Exactly two things, and nothing else:

- `status: stale-pending-review` in an affected page's frontmatter.
- The text between `<!-- index:begin -->`/`<!-- index:end -->` and between
  `<!-- related:begin -->`/`<!-- related:end -->`, both maintained by
  `memline wiki index`. No byte outside the markers is ever modified, and
  tests hold that, including on a frozen Blog article. The index never
  creates a file: a shelf listing exists only where a README already carries
  the markers, and `content/docs/.nav.yml` is hand-written, never generated.

Everything else in `content/` is approved published work. A program that can
write into it can also damage it, so the reach is kept narrow enough to be
enforced rather than intended.

## 5. Present and record verdicts

Generate a review view (Markdown, grouped by theme, verdicted entries dropped)
for the user, then append one ledger line per verdict:

```
run-NNN-S001 | accepted|rejected|deferred | YYYY-MM-DD | note
```

Rejection permanently suppresses that suggestion and its evidence set; the
topic may return only with substantially different evidence. Deferred items may
be re-raised without new evidence — and a compile should report how much new
evidence a deferred topic has accumulated since it was deferred.

For each accepted topic append to `accepted-topics.jsonl`: approved scope, the
absorbed suggestions, and the union of their evidence. **Drafting uses the
union**, not the single suggestion's evidence.

## 6. Close the run

```
memline wiki close-run state/compile.json --started-at <run_started_at>
```

Only when everything above is written: flip `run.md` to `status: complete` and
update `state/compile.json` — `last_compile_at` = `run_started_at` (not the end
time), new source hashes, new boundary ids, `next_run` bumped. Never advance
the cursor after a failed or interrupted run. Do not write per-suggestion
memories during a compile; at most one completion memory afterwards.
