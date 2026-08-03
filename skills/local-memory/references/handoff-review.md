# Session Handoff Review (过期审查)

Read this file ONLY when the user asks for an end-of-session review — e.g.
"做一次过期审查", "handoff review", "收尾审查" — or explicitly tells you the
session is being retired. During normal task execution none of this applies:
background judges mark things silently, and every mark waits for this moment.

The required order is **filter this session's writes → proactively maintain
them → run review → dispose remaining flags → run review again until clean**.
Do not use the first `review` call as a substitute for the preflight pass.

## The cardinal rule

**Every disposal decision is YOURS, made after personally reading the entry.**
The background judge's verdicts, confidence scores, and `suggested` strings
are advisory triage — they order your attention, they never decide. Never
wire a judge verdict straight to a disposal command without reading the
memory text yourself. (Design invariant #6, user-mandated 2026-07-21.)

## Step 1 — resolve and filter the current session

Resolve the current session ID from the same hard context signal used by
`memline` (`MEMLINE_SESSION_ID`, `MEM0_SESSION_ID`, `AGENT_SESSION_ID`,
`CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CLAUDE_SESSION_ID`,
`CLAUDE_CODE_SESSION_ID`, or `CLAUDECODE_SESSION_ID`). If no hard session ID is
available, stop and obtain the explicit current session ID; **never broaden the
preflight to every memory written by an `agent_id`**.

List only this session's writes before calling `review`:

```bash
memline list --filter run_id=<current-session-id> --page-size 500
```

Read every returned memory in full and continue with `--page 2`, `--page 3`,
and so on until a page is empty. The filter is the ownership boundary for the
preflight; semantic search may supply evidence, but must not replace the
complete session-scoped enumeration.

## Step 2 — proactively maintain the filtered writes

Personally check every filtered memory before asking the background review for
advice. Do not wait for a judge flag when the problem is already evident:

1. **Safety:** redact plaintext credentials or secret material in place; keep
   only a pointer to the credential location, then verify the literal is absent
   even with `--include-superseded`.
2. **Correctness:** compare the memory against the session's actual evidence and
   final outcome. Fix wrong dates, actors, counts, causal certainty, paths, or
   non-English narrative with `update`; do not preserve a known factual error
   merely because it is historical.
3. **Currentness:** retire an earlier progress snapshot when a later result from
   this session replaces it. Prefer one accurate final-state memory and explicit
   supersession/invalidation over multiple competing "current" answers. Preserve
   genuinely historical facts as history rather than rewriting their event.
4. **Necessity and lifetime:** remove fresh, derivable activity narration only
   when it has no durable payload; use reversible invalidation or TTL for
   event-scoped state. Keep and protect hard-won findings, corrections,
   safeguards, canonical locations, and standing user constraints.

Use `get <id> --resolve-head` and targeted `search ... --include-superseded` when
lineage or competing answers need evidence. Every decision remains the current
agent's decision; the preflight is not an automatic deletion pass.

Write or update the final handoff memory **before** the first `review` call so it
is included in the judged write set. If preflight maintenance creates another
memory, include it in the same inspection before continuing.

## Step 3 — collect remaining review work

```bash
memline review --wait     # auto-detects the same current session
```

The output has four work lists:

| section | what it is | raised by |
|---|---|---|
| `writes` | everything this session added | you |
| `open_pairs` | displacement suspicions raised by **this session's** writes: an old entry may be superseded by one of them. Pairs raised by other sessions are not listed here — see the backlog walk in Step 4 | background judge |
| `self_flags` | this session's own writes flagged by the self-check judges: **safety** (a suspected embedded plaintext credential), **correctness** (timestamp/attribution mismatch, or non-English narrative — the store embeds English-only, so Chinese prose retrieves poorly), and **necessity** (BORN_UNNECESSARY: activity narration / commit restatement / repo-readable fact — or EXPIRING: progress tick / event-scoped coordination). Emitted already sorted by disposition priority (safety → correctness → necessity). | background judge |
| `ttl_expired` | entries whose TTL deadline fired and who left the search pool. Any session may dispose these, and the verdict requires it | harvest loop |

Each flagged item carries a `suggested` block keyed to that pair's own
verdict — not just its kind:

| field | what it gives you |
|---|---|
| `means` | what this verdict actually claims about the entry |
| `fix` | the disposition that resolves it, as a command template |
| `keep` / `partial` | what must survive the fix, or the alternative disposition |
| `dismiss_only_if` | the one condition under which dismissing is correct |
| `warning` | dismissal is permanent for this exact text version |

Use it as a menu, not as a decision. `dismiss_only_if` is the field to read
twice: see "dismissal is not acknowledgement" below.

The `writes` list should match the session-scoped preflight set after its
maintenance. If it does not, stop and resolve the session/filter mismatch before
disposing anything.

## Step 4 — dispose, per kind

One entry can carry several flags at once (the judges are independent, not
mutually exclusive — e.g. an entry can be both `safety` and `necessity`).
Dispose in this fixed order so a later action never strands an earlier
concern:

**safety → correctness → necessity → displacement → TTL expiries.**

The reason for the order: a `safety` redaction and a `correctness` fix are
both `update`s that *keep* the entry; do them first. Only then decide
`necessity` — if you invalidate first, the plaintext or the wrong fact is
still retrievable via `--include-superseded`, so the leak/error outlives the
"disposal". Displacement is judged against the surviving text, so settle the
self-flags before it.

**1. Safety flags** (`self_flags`, kind `safety`) — HIGHEST PRIORITY: the entry
may embed a plaintext credential (password, token, key, one-time code). The
store rule is that entries reference a secret's LOCATION, never its value.
`update <memory_id> "<same text, secret replaced by a pointer to its
file/env var>"` redacts in place (keeps id/links/history, expires the flag),
then verify `search "<secret literal>" --include-superseded` returns nothing.
If the flag is a false alarm (a location pointer, hash, or public identifier),
`stale dismiss`. The judge's reason never repeats the suspected value by
design. Redact even entries already invalidated — the value survives pool exit.

**2. Correctness flags** (`self_flags`, kind `correctness`): the fact is true
but written wrong — a date/actor mismatch (TIMESTAMP/ATTRIBUTION_SUSPECT) or a
non-English narrative (LANGUAGE_SUSPECT; entries must be English prose because
the store's embedding is English-only — only technical identifiers may keep
non-English characters). Fix with `update` (rewrite the date/actor, or
re-express the entry in English; this also expires the flag); `stale dismiss`
if the flag is mistaken. Never expire these.

When rewriting a LANGUAGE_SUSPECT entry, preserve verbatim: paths, commands,
flags, hosts, model/artifact names, every number, memory-id cross-references,
Chinese proper nouns (people, products, filenames), and quoted Chinese source
material. Correction markers are load-bearing — an entry that says an earlier
belief was wrong must still say so with equal force after the rewrite. Length
cap is 600 characters, and English expands relative to Chinese, so compress
phrasing rather than dropping facts.

### Dismissal is not acknowledgement

`stale dismiss` asserts **the judge was wrong**. It is permanent for that exact
text version: once dismissed, no future judgment can raise that finding again
unless the entry's text changes. It is not a way to mark a flag as seen, and
"the facts in this entry are correct" is not grounds for dismissing a
LANGUAGE_SUSPECT — that flag is about the language the entry is *written in*,
not whether it is true.

This is not hypothetical. An audit on 2026-07-28 found ten LANGUAGE_SUSPECT
flags batch-dismissed in seven seconds during a handoff, none of the entries
rewritten; because dismissal is permanent per text version, those entries were
unreachable by the judge until they were rewritten by hand months later. If a
flag is real but you are not going to fix it now, leave it open — an open flag
is recoverable, a dismissed one is not.

**3. Necessity flags** (`self_flags`, kind `necessity`):
```bash
memline delete <memory_id> --force        # born-unnecessary AND written by this session: remove outright
memline stale confirm <pair_id>           # born-unnecessary but keep the audit trail: reversible expiry, out of pool now
memline stale ttl <pair_id> [--days 7]    # EXPIRING but the event is still live: keep for now, expire at deadline
memline stale dismiss <pair_id>           # false flag
memline update <memory_id> "<rewritten>"  # half-redundant: strip the derivable part, keep the real payload
```

**4. Displacement pairs** (`open_pairs`) — handle last:
```bash
memline stale confirm <pair_id>                       # old entry really is superseded -> invalidate (reversible)
memline stale merge <pair_id> "<consolidated text>"   # new entry ADDS detail: newer absorbs both, older retires
memline update <old_id> "<corrected text>"            # the old entry just needs fixing
memline stale dismiss <pair_id>                       # false suspicion
```

`review` surfaces only the pairs tied to the current session. The store also
holds pairs raised by other sessions' writes: those never enter your actionable
count, are refused non-interactively, and therefore stay open indefinitely while
remaining invisible in the summary counters. Before declaring the review done,
walk the whole backlog and resolve each pair's OLD side:

```bash
memline stale list        # full backlog, not just this session's pairs
memline get <old_id>      # metadata carries the writing source and session
```

You walk the backlog to confirm that none of it is yours — not to work through
it. **A session disposes only its own writes.** A different session id is a
different owner even under the same agent name: concurrent agents write to one
store as the same `writer_agent_id`, and an earlier conversation of "you" is
just as much another session. Recognizing the content is not ownership. Do not
`--force` your way through those pairs to tidy up history you remember writing;
leave them, and report the backlog's size and owners to the user instead.

The obligation runs the other way. A finished session can never come back to
dispose anything, so **your own writes must be fully handled before this session
ends**. Whatever you leave open becomes permanently un-disposable unless the
user personally authorizes an override later. That is why this review is
mandatory at handoff rather than best-effort, and why the preflight in Step 1
is scoped to your session and must be exhaustive within it.

If the latest three opening displacement suspicions against the same unchanged
memory were all dismissed and came from distinct `new_id` values, a reviewer
may suppress only future displacement judging temporarily:

```bash
memline stale protect <memory_id> --days 30 --reason "repeated false positive: <shared pattern>"
memline stale protected list
memline stale unprotect <memory_id>
```

Protection lasts at most 90 days, records setter and a 1-500 character reason,
clears on text update or invalidation, and never suppresses safety, correctness,
or necessity checks. Any newer non-dismissed disposition interrupts eligibility;
the core setter has no force bypass.

**TTL expiries** (`ttl_expired`): the entry already left the pool at its
deadline. `stale confirm <pair_id>` accepts that; `stale ttl <pair_id>` renews
it and re-enters the pool. Any session may dispose these, no --force needed —
and because every session may, every session must: an unreviewed TTL expiry
blocks the verdict. Granting authority to all while obliging none is how a
backlog nobody owns becomes a backlog nobody clears.

Read each one before accepting. A deadline firing is evidence that its writer
expected the entry to be event-scoped, not proof that it was: a durable fact
handed a TTL by mistake will expire on schedule and silently leave the pool.
Renew those (`stale ttl --days N`) instead of accepting.

## Protection list — scrutinize before confirming

Corrections/retractions ("earlier conclusion X was wrong"), hard-won lessons
(footguns, mandatory safeguards), costly audit/debugging conclusions,
credential/key/artifact location pointers, canonical repo/endpoint URLs, and
standing constraints (resource whitelists, user-set rules). The judge is
prompted to spare them; if one still shows up incorrectly, `stale dismiss`.
Repeated false positives may use the bounded displacement protection above;
there is no permanent or all-kind immunity.

## Semantics and safety rails

- Everything except `delete` is reversible: invalidation undoes with
  `revive <id>`, TTL/expiry undoes with `ttl <id> --clear`. Text, history,
  and audit manifests survive every path.
- `delete` is for this session's own fresh born-unnecessary entries only; it
  refuses supersession-chain participants (auto-downgrades to reversible
  expiry, reported as `downgraded_to_expiry`).
- Authority: non-interactive sessions may only dispose suspicions raised by
  their own writes; cross-session backlog gets a default-No confirmation prompt
  in an interactive session, or requires `--force` after explicit user approval.
  (`ttl_expired` items are exempt from that ownership rule — but not from the
  verdict: see Step 5.)
- A stderr banner counting open suspicions appears on hygiene commands
  (`review`/`stale`/`event`/`status`) — it is pressure gauge, not a to-do
  during task execution.
- Manual supersession outside review: `invalidate <id> --by <new_id>`,
  chain inspection with `get <id> --resolve-head`.

## Step 5 — repeat until the current session is clean

The acceptance criterion is computed for you — do not re-derive it by reading
the four lists:

```bash
memline review --wait --check     # exit 0 = pass, exit 2 = blocked
```

The output carries `verdict` (`pass` / `blocked`) and, when blocked, a
`blocking` list naming each unmet item with its ids and the command to clear
it. **`verdict: pass` is the definition of a finished handoff.** It means
everything THIS session wrote has been handled:

- `pending_stale_checks` is zero, so the judges are done looking at your writes;
- no `safety`, `correctness`, or `necessity` flag on your own writes remains;
- every displacement pair *your writes raised* is disposed;
- no queued write of your own failed to land in the store;
- no TTL expiry anywhere is left unreviewed — those carry no owner, so the
  session at handoff is the one obliged to clear them.

Leftover pairs owned by other sessions persist and keep showing in the banner.
They are deliberately excluded from the verdict: disposing them is not this
session's job, and in a store with concurrent writers a verdict that waited on
them could never go green. Report the backlog's size to the user and leave it.

Never substitute your own reading of the counters for the verdict. `self_flags`
and `pending_stale_checks` alone say only that the judges have nothing further
to add about your writes; they are silent about the pairs your writes raised,
which is where an unfinished handoff usually hides.

If any `add` or `update` occurs after that pass — including a handoff-summary
write — run the review again. Do not create an unreviewed final memory after
declaring the session clean.

For the underlying data model (suspicion kinds, verdict vocabulary,
confidence floors, TTL loop, delete guard), read
[staleness-design.md](staleness-design.md).
