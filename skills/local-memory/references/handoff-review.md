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
`mem0-local` (`MEM0_LOCAL_SESSION_ID`, `MEM0_SESSION_ID`, `AGENT_SESSION_ID`,
`CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CLAUDE_SESSION_ID`,
`CLAUDE_CODE_SESSION_ID`, or `CLAUDECODE_SESSION_ID`). If no hard session ID is
available, stop and obtain the explicit current session ID; **never broaden the
preflight to every memory written by an `agent_id`**.

List only this session's writes before calling `review`:

```bash
mem0-local list --filter run_id=<current-session-id> --page-size 500
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
mem0-local review --wait     # auto-detects the same current session
```

The output has four work lists:

| section | what it is | raised by |
|---|---|---|
| `writes` | everything this session added | you |
| `open_pairs` | displacement suspicions: an old entry may be superseded by one of this session's writes | background judge |
| `self_flags` | this session's own writes flagged by the self-check judges: **safety** (a suspected embedded plaintext credential), **correctness** (timestamp/attribution mismatch, or non-English narrative — the store embeds English-only, so Chinese prose retrieves poorly), and **necessity** (BORN_UNNECESSARY: activity narration / commit restatement / repo-readable fact — or EXPIRING: progress tick / event-scoped coordination). Emitted already sorted by disposition priority (safety → correctness → necessity). | background judge |
| `ttl_expired` | entries whose TTL deadline fired and who left the search pool | harvest loop (any session may dispose these) |

Each flagged item carries a `suggested` field listing the applicable
commands — use it as a menu, not as a decision.

The `writes` list should match the session-scoped preflight set after its
maintenance. If it does not, stop and resolve the session/filter mismatch before
disposing anything.

## Step 4 — dispose, per kind

One entry can carry several flags at once (the judges are independent, not
mutually exclusive — e.g. an entry can be both `safety` and `necessity`).
Dispose in this fixed order so a later action never strands an earlier
concern:

**safety → correctness → necessity → displacement** (TTL expiries whenever).

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

**3. Necessity flags** (`self_flags`, kind `necessity`):
```bash
mem0-local delete <memory_id> --force        # born-unnecessary AND written by this session: remove outright
mem0-local stale confirm <pair_id>           # born-unnecessary but keep the audit trail: reversible expiry, out of pool now
mem0-local stale ttl <pair_id> [--days 7]    # EXPIRING but the event is still live: keep for now, expire at deadline
mem0-local stale dismiss <pair_id> [--pin]   # false flag (see protection list below)
mem0-local update <memory_id> "<rewritten>"  # half-redundant: strip the derivable part, keep the real payload
```

**4. Displacement pairs** (`open_pairs`) — handle last:
```bash
mem0-local stale confirm <pair_id>                       # old entry really is superseded -> invalidate (reversible)
mem0-local stale merge <pair_id> "<consolidated text>"   # new entry ADDS detail: newer absorbs both, older retires
mem0-local update <old_id> "<corrected text>"            # the old entry just needs fixing
mem0-local stale dismiss <pair_id> [--pin]               # false suspicion; --pin = never judge this memory again
```

**TTL expiries** (`ttl_expired`, dispose whenever): the entry already left the
pool at its deadline. `stale confirm <pair_id>` accepts that; `stale ttl
<pair_id>` renews it and re-enters the pool. Any session may dispose these, no
--force needed.

## Protection list — never flag-confirm these

Corrections/retractions ("earlier conclusion X was wrong"), hard-won lessons
(footguns, mandatory safeguards), costly audit/debugging conclusions,
credential/key/artifact location pointers, canonical repo/endpoint URLs, and
standing constraints (resource whitelists, user-set rules). The judge is
prompted to spare them; if one still shows up flagged, `stale dismiss --pin`.

## Semantics and safety rails

- Everything except `delete` is reversible: invalidation undoes with
  `revive <id>`, TTL/expiry undoes with `ttl <id> --clear`. Text, history,
  and audit manifests survive every path.
- `delete` is for this session's own fresh born-unnecessary entries only; it
  refuses supersession-chain participants (auto-downgrades to reversible
  expiry, reported as `downgraded_to_expiry`).
- Authority: non-interactive sessions may only dispose suspicions raised by
  their own writes; cross-session backlog belongs to the user, or requires
  `--force` after their explicit approval. (`ttl_expired` items are exempt.)
- A stderr banner counting open suspicions may appear on any command — it is
  pressure gauge, not a to-do during task execution.
- Manual supersession outside review: `invalidate <id> --by <new_id>`,
  chain inspection with `get <id> --resolve-head`.

## Step 5 — repeat until the current session is clean

Leftover pairs persist and keep showing in the banner; that is acceptable
for cross-session items you lack authority over. After all permitted
dispositions, run `mem0-local review --wait` again. A clean handoff requires:

- `pending_stale_checks` is zero;
- no actionable safety, correctness, necessity, or displacement item raised by
  the current session remains;
- every intentionally unresolved cross-session item is explicitly recognized as
  outside the current agent's authority.

If any `add` or `update` occurs after that pass — including a handoff-summary
write — run the review again. Do not create an unreviewed final memory after
declaring the session clean.

For the underlying data model (suspicion kinds, verdict vocabulary,
confidence floors, TTL loop, delete guard), read
[staleness-design.md](staleness-design.md) and the lifecycle design doc at
`/workspace/reference/mem-lifecycle-design-2026-07-20.md`.
