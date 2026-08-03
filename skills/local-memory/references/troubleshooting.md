# Local Memory Troubleshooting

## Entrypoints

Use `memline` first. It is installed on PATH through:

```bash
command -v memline
ls -l "$(command -v memline)"
```

In a workspace-local install, PATH usually points to a small wrapper under the
workspace memory directory, for example:

```text
<home>/.local/bin/memline -> <workspace>/.agent-memory/bin/memline
```

The wrapper resolves symlinks and runs the configured Python environment, often:

```text
<workspace>/.agent-memory/store/venv/bin/python
python -m memline.cli
```

The wrapper sets `MEMLINE_CONFIG` and `PYTHONPATH`, then loads the reusable
implementation from the Git submodule:

```text
<workspace>/.agent-memory/projects/memline/src/memline/
<workspace>/.agent-memory/config.toml
```

If PATH is missing after a restart, run:

```bash
"$HOME/.local/bin/memline" status
```

## Store Layout

Find the active store with:

```bash
memline status --json
```

For this workspace layout, all runtime state is under:

```text
<workspace>/.agent-memory/store/
```

Important paths:

- Qdrant server deployment (used when `[vector_store]` host/port is set):
  `<store>/qdrant-server/` — binary, `config.yaml`, data, log, and the
  `qdrantctl.sh {start|stop|status}` control script; HTTP on `127.0.0.1:6333`
- Embedded qdrant local path (fallback when `[vector_store]` is unset): `<store>/qdrant`
- Mem0 history DB: `<store>/history.db`
- Fastembed cache: `<store>/model-cache/fastembed`
- Mem0 redirected home/config: `<store>/home`, `<store>/mem0`
- CLI lock: `<store>/cli.lock`
- Optional daemon socket: `<store>/daemon.sock`
- Optional daemon PID/log: `<store>/daemon.pid`, `<store>/daemon.log`
- Secret env file: `<store>/.env`

Never print or copy `.agent-memory/store/.env`.

The active workspace profile is tracked at:

```text
<workspace>/.agent-memory/config.toml
```

This file contains paths and provider names only; secrets stay in the env file.

## Git Boundary

The database and runtime files are intentionally not git-managed. `.gitignore` excludes `.agent-memory/store/.env`, `history.db`, `cli.lock`, `venv/`, `home/`, `mem0/`, `model-cache/`, and `qdrant/`.

The git-managed pieces are usually the CLI wrapper, the
`.agent-memory/projects/memline` submodule pointer, skill files, workspace
config, manifests, and `.gitignore`.

External audit manifests live under:

```text
<workspace>/.agent-memory/manifests/
```

Live `add`, `update`, and `delete` operations append monthly
`live-YYYY-MM.jsonl` rows. Historical imports and metadata backfills use
`ledger-YYYY-MM.jsonl` and `metadata-backfill-*.jsonl`. These JSONL files are
the git-friendly audit source; the database remains a runtime index.

## Concurrency

Server mode (`[vector_store]` host/port set in `config.toml`): the qdrant server natively supports concurrent clients. `memline` still serializes commands with `cli.lock` for history/manifest consistency; if a command waits, another memory command is active. The server process must be running (`qdrantctl.sh status`); after a machine/WSL restart, start it before any memory command.

Local-path mode (no `[vector_store]` section): the embedded store cannot be opened safely by multiple processes at the same time; `cli.lock` and the optional daemon serialize access.

Correctness boundary:

- Safe: all agents use `memline` inside this WSL workspace.
- Unsafe (local-path mode): agents directly import Mem0/Qdrant against the same path, or another machine/Windows process opens the same Qdrant directory.
- Removing `[vector_store]` from `config.toml` silently switches to whatever data `<store>/qdrant` holds — after a workspace has migrated to server mode that directory is an empty stub, so do not remove the section casually; migration and rollback specifics are recorded in mem0.

## Optional Daemon Checks

Use the daemon when repeated one-shot CLI commands are dominated by cold start:

```bash
memline daemon start
memline daemon status
memline daemon stop
```

To use the direct one-shot path for comparison or debugging, stop the daemon
first and then run:

```bash
MEMLINE_NO_DAEMON=1 memline search "test"
```

If the daemon fails to start, inspect `<store>/daemon.log`. If `daemon status`
shows a stale socket or PID after a crash, run `memline daemon stop` first;
newer `daemon start` also tries to recover a stale daemon pid automatically.
Do not manually remove runtime files unless `daemon stop` cannot run and the
process is confirmed gone.

Some managed agent sandboxes can see `<store>/daemon.sock` but are not allowed
to connect to it (`PermissionError: Operation not permitted`). In that case,
stop the daemon before using the direct path, or run the memory command outside
the sandbox. Do not leave an unreachable daemon running, because it owns the
local Qdrant lock and direct commands will wait behind it.

Recovery checklist:

```bash
memline daemon status --json
memline daemon stop
MEMLINE_NO_DAEMON=1 memline search "health check"
```

If a command still waits after stopping the daemon, inspect which process owns
`<store>/cli.lock`; only terminate processes you can identify as stale
`memline.daemon --serve` instances or abandoned `memline` commands.

## Basic Checks

```bash
memline status --json
memline embed-test "hello"
memline search "test" --json
```

If `memline` is missing, check:

```bash
which memline
ls -l "$HOME/.local/bin/memline"
ls -l "<workspace>/.agent-memory/bin/memline"
```

If API-key-dependent features fail, use `status --json` and check only `api_key_set`; do not print the key.

## Add Failures vs Empty Results

With `mem0ai>=2.0.11`, LLM-extraction failures during `add` raise instead of silently returning empty `results`:

- CLI error / exit code 1 (`add failed in mem0 backend: ...` or `memline daemon add failed: ...`): the LLM call or extraction parsing failed. Check the LLM provider key/network, then retry; split long or dense input into shorter atomic entries.
- Success envelope with `"results": []`: the backend processed the input but stored nothing — the fact was deduplicated against an existing memory or contained nothing extractable. This is not an error.

Failed `add` attempts still append an audit row to `.agent-memory/manifests/live-YYYY-MM.jsonl` with an `{"error": ...}` result payload, so audits distinguish failed writes from empty ones.

## Rollback Checks

To confirm interrupted ledger imports are gone:

```bash
memline list --filter source=agent-memory-ledger --page-size 5 --json
```

An empty `data` list means no visible ledger-import entries remain for the default `workspace` user.
