# mem0-local

`mem0-local` is a local-first Mem0 CLI wrapper. It keeps runtime state under a configured local store, uses Qdrant local path mode, and writes audit metadata for timestamps, writer identity, sessions, schema version, and updates.

## Architecture

The abstraction is split into four boundaries:

- Package code: command behavior, metadata policy, config parsing, import helpers.
- Workspace profile: absolute local paths, collection name, model/provider settings.
- Runtime store: Qdrant path data, Mem0 history DB, model cache, venv, lock file, and secrets.
- Agent discovery: skill docs and wrappers that tell Codex/Claude to call `mem0-local`.

Only the package and workspace profile are meant to be committed. Runtime store contents stay local.

## Install

For a new local workspace:

```bash
python -m venv .agent-memory/store/venv
.agent-memory/store/venv/bin/pip install git+https://github.com/linzihan-tech/mem0-local.git
export MEM0_LOCAL_CONFIG="$PWD/.agent-memory/config.toml"
```

For development from a checkout:

```bash
.agent-memory/store/venv/bin/pip install -e .
```

Put provider secrets in the configured env file, for example `.agent-memory/store/.env`. Do not commit that file.

## Commands

```bash
mem0-local status
mem0-local add "accurate atomic memory text"
mem0-local search "semantic query"
mem0-local list --filter agent_id=codex
mem0-local get <memory_id>
mem0-local update <memory_id> "corrected memory text"
mem0-local delete <memory_id>
```

Routine agents should call `add` with only the memory text. The CLI auto-detects agent/session context when possible, writes timestamps and schema metadata, and returns JSON in agent contexts.

Use `list --filter ...` for structured audits by metadata fields such as `agent_id`, `run_id`, `source`, `session_id`, `created_at`, or `ingested_at`. Keep `search` for semantic retrieval.

## Configuration

The CLI locates configuration in this order:

1. `MEM0_LOCAL_CONFIG`
2. `.agent-memory/config.toml` found from the current directory upward
3. `~/.config/mem0-local/config.toml`

See `examples/config.toml` for a portable template.

Runtime data stays under `.agent-memory/store/` and remains excluded from git.
