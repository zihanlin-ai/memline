# Session Handoff Review (过期审查)

Read this file ONLY when the user asks for an end-of-session review — e.g.
"做一次过期审查", "handoff review", "收尾审查" — or explicitly tells you the
session is being retired. During normal task execution none of this applies:
background judges mark things silently, and every mark waits for this moment.

## The cardinal rule

**Every disposal decision is YOURS, made after personally reading the entry.**
The background judge's verdicts, confidence scores, and `suggested` strings
are advisory triage — they order your attention, they never decide. Never
wire a judge verdict straight to a disposal command without reading the
memory text yourself. (Design invariant #6, user-mandated 2026-07-21.)

## Step 1 — collect

```bash
mem0-local review --wait     # --wait lets pending background judgments land (<=120s)
```

The output has four work lists:

| section | what it is | raised by |
|---|---|---|
| `writes` | everything this session added | you |
| `open_pairs` | displacement suspicions: an old entry may be superseded by one of this session's writes | background judge |
| `self_flags` | this session's own writes flagged by the self-check judges: **safety** (a suspected embedded plaintext credential), **correctness** (timestamp/attribution mismatch), and **necessity** (BORN_UNNECESSARY: activity narration / commit restatement / repo-readable fact — or EXPIRING: progress tick / event-scoped coordination). Emitted already sorted by disposition priority (safety → correctness → necessity). | background judge |
| `ttl_expired` | entries whose TTL deadline fired and who left the search pool | harvest loop (any session may dispose these) |

Each flagged item carries a `suggested` field listing the applicable
commands — use it as a menu, not as a decision.

## Step 2 — dispose, per kind

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
but its date or actor is written wrong (timestamp/attribution mismatch) — fix
with `update` (which also expires the flag); `stale dismiss` if the flag is
mistaken. Never expire these.

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

## Step 3 — close out

Leftover pairs persist and keep showing in the banner; that is acceptable
for cross-session items you lack authority over. End by adding the session's
handoff memory (`mem0-local add`) as usual.

For the underlying data model (suspicion kinds, verdict vocabulary,
confidence floors, TTL loop, delete guard), read
[staleness-design.md](staleness-design.md) and the lifecycle design doc at
`/workspace/reference/mem-lifecycle-design-2026-07-20.md`.
