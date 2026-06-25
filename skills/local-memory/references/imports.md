# Historical Ledger Imports

## Preferred Granularity

Do not import a whole Markdown ledger file as one memory.

Preferred unit: one top-level ledger entry per memory. If a top-level bullet has indented child bullets, keep the child bullets with the parent entry. This preserves the original ledger structure without fragmenting context too aggressively.

For strict line-by-line migration requested by the user, use line mode: preserve every non-empty, non-heading Markdown line as one memory, keep line order, and include enough metadata to audit the original source.

## Required Metadata

For migrated ledger entries, include at least:

```text
source=agent-memory-ledger
session_id=ledger-YYYY-MM
writer_agent_id=ledger-importer
origin=ledger_import
memory_schema_version=2
ledger_file=.agent-memory/YYYY-MM-DD.md
ledger_date=YYYY-MM-DD
line_no=<start line>
sequence=<monotonic import/order number>
timestamp_source=<explicit_heading|explicit_text|file_date_sequence>
```

Default historical ledger imports use inference so Mem0 can add, update, delete, or skip memories as newer ledger entries arrive in chronological order. Use `--no-infer` only when the user explicitly wants a raw audit index rather than maintained memory.

Top-level scope for ledger imports is normalized as
`agent_id=ledger-importer` and `run_id=ledger-YYYY-MM`; the matching metadata
field is `session_id=ledger-YYYY-MM`.

Each import writes a git-managed JSONL manifest under `.agent-memory/manifests/`.
Every manifest row keeps the original submitted add input regardless of how Mem0
extracts, updates, skips, or returns the memory:

```text
source_entry.original_body   original Markdown line/block
source_entry.add_input       exact text submitted to Mem0 add()
timestamps.*                 derived ledger/import timestamps
memory_result                raw Mem0 add() result
events/memory_ids            flattened audit helpers
result_memories              extracted or updated memory text returned by Mem0
```

Existing memories were backfilled to the same schema. The git-managed audit
manifest is `.agent-memory/manifests/metadata-backfill-*.jsonl`.

## Timestamp Policy

`mem0-local add` automatically records current `created_at`, `ledger_timestamp`, and `ingested_at`.

For one-off manual historical entries, override event time with `--timestamp`
only when deriving a historical timestamp. Do not pass schema/source/session
metadata for routine live memories.

```bash
mem0-local add "old memory text" --timestamp "2026-05-18T00:00:15+08:00"
```

For real ledger migration, prefer the import helper below. It derives ledger
metadata, source locations, timestamps, monotonic sequence fields, and manifest
rows itself.

Timestamp sources:

- `explicit_heading`: use a surrounding heading such as `## 2026-05-18 20:47:42`.
- `explicit_text`: use a timestamp written in the entry text itself; `YYYY-MM-DD HH:MM` and `YYYY-MM-DD HH:MM:SS` are both accepted.
- `file_date_sequence`: if no precise time exists, use file date plus line/sequence to create stable ordering. This is not a recovered real write time.

The current import helper defaults to line mode and inference enabled:

```bash
/workspace/.agent-memory/store/venv/bin/mem0-local-ingest-ledger path/to/ledger.md --month 2026-05 --mode line --dry-run
```

For a raw audit-only import, add `--no-infer` only when the user explicitly asks
for exact raw text rather than maintained memories.

## Known Historical Limit

Older `.agent-memory/2026-05-*.md` files preserve file dates and some explicit timestamps, but many individual entries do not retain true write time. Git history only shows the 2026-06-05 auto snapshot for many May files, so do not treat git blame time as original event time.

## Dry Run First

Before large imports, dry-run and inspect the count and sample entries. The
manifest is the rollback/audit source and should stay in git. If an import is
interrupted, verify residual entries with:

```bash
mem0-local list --filter source=agent-memory-ledger --page-size 5 --json
```
