# Local Memory Troubleshooting

## Entrypoints

Use `mem0-local` first. It is installed on PATH through:

```bash
command -v mem0-local
ls -l "$(command -v mem0-local)"
```

In a workspace-local install, PATH usually points to a small wrapper under the
workspace memory directory, for example:

```text
<home>/.local/bin/mem0-local -> <workspace>/.agent-memory/bin/mem0-local
```

The wrapper resolves symlinks and runs the configured Python environment, often:

```text
<workspace>/.agent-memory/store/venv/bin/python
python -m mem0_local.cli
```

The wrapper sets `MEM0_LOCAL_CONFIG` and `PYTHONPATH`, then loads the reusable
implementation from the Git submodule:

```text
<workspace>/.agent-memory/projects/mem0-local/src/mem0_local/
<workspace>/.agent-memory/config.toml
```

If PATH is missing after a restart, run:

```bash
"$HOME/.local/bin/mem0-local" status
```

## Store Layout

Find the active store with:

```bash
mem0-local status --json
```

For this workspace layout, all runtime state is under:

```text
<workspace>/.agent-memory/store/
```

Important paths:

- Qdrant local path: `<store>/qdrant`
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
`.agent-memory/projects/mem0-local` submodule pointer, skill files, workspace
config, manifests, and `.gitignore`.

## Concurrency

Qdrant local path mode cannot be opened safely by multiple processes at the same time. `mem0-local` serializes commands with `cli.lock`; if a command waits, another memory command is active.

Correctness boundary:

- Safe: all agents use `mem0-local` inside this WSL workspace.
- Safe: a running `mem0-local daemon` owns the local Qdrant path, while normal
  CLI commands talk to the daemon through `<store>/daemon.sock`.
- Unsafe: agents directly import Mem0/Qdrant against the same path, or another machine/Windows process opens the same Qdrant directory.
- For high-throughput concurrent access, switch to Qdrant server mode.

## Optional Daemon Checks

Use the daemon when repeated one-shot CLI commands are dominated by cold start:

```bash
mem0-local daemon start
mem0-local daemon status
mem0-local daemon stop
```

To use the direct one-shot path for comparison or debugging, stop the daemon
first and then run:

```bash
MEM0_LOCAL_NO_DAEMON=1 mem0-local search "test"
```

If the daemon fails to start, inspect `<store>/daemon.log`. If `daemon status`
shows a stale socket or PID after a crash, `daemon stop` cleans up missing
process state; otherwise remove only the stale `<store>/daemon.sock` and
`<store>/daemon.pid` files.

## Basic Checks

```bash
mem0-local status --json
mem0-local embed-test "hello"
mem0-local search "test" --json
```

If `mem0-local` is missing, check:

```bash
which mem0-local
ls -l "$HOME/.local/bin/mem0-local"
ls -l "<workspace>/.agent-memory/bin/mem0-local"
```

If API-key-dependent features fail, use `status --json` and check only `api_key_set`; do not print the key.

## Rollback Checks

To confirm interrupted ledger imports are gone:

```bash
mem0-local list --filter source=agent-memory-ledger --page-size 5 --json
```

An empty `data` list means no visible ledger-import entries remain for the default `workspace` user.
