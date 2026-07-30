# mem0-local

`mem0-local` is a local-first Mem0 CLI wrapper. It keeps runtime state under a configured local store, backs vectors with Qdrant (embedded local path mode by default, or a local Qdrant server via `[vector_store]`), and writes audit metadata for timestamps, writer identity, sessions, schema version, and updates.

## Architecture

The abstraction is split into four boundaries:

- Package code: command behavior, metadata policy, config parsing, import helpers.
  Internally, `runtime.py` owns bootstrap (env setup, cross-process lock, Mem0
  client build) and `ops.py` is the single registry of store operations: the
  CLI's direct path and the daemon execute the same handlers, and per-op
  transport metadata (timeout, LLM slot, exclusive store access) lives in the
  same registry. Adding an op means one entry in `ops.py`, not parallel edits
  in `cli.py` and `daemon.py`.
- Workspace profile: absolute local paths, collection name, model/provider settings.
- Runtime store: Qdrant path data, Mem0 history DB, model cache, venv, lock file, and secrets.
- Agent discovery: skill docs and wrappers that tell Codex/Claude to call `mem0-local`.

Only the package and workspace profile are meant to be committed. Runtime store contents stay local.

## Install

For a new local workspace:

```bash
python -m venv .agent-memory/store/venv
# 1. Vendored mem0ai FIRST — the official PyPI mem0ai will not work
#    (this repo depends on workspace modifications in vendor/mem0ai).
.agent-memory/store/venv/bin/pip install "mem0ai @ git+https://github.com/linzihan-tech/mem0-local.git#subdirectory=vendor/mem0ai"
# 2. The CLI package itself.
.agent-memory/store/venv/bin/pip install git+https://github.com/linzihan-tech/mem0-local.git
export MEM0_LOCAL_CONFIG="$PWD/.agent-memory/config.toml"
```

For development from a checkout:

```bash
.agent-memory/store/venv/bin/pip install -e ./vendor/mem0ai -e .
```

The CLI fails fast with a clear error if the official mem0ai package is
installed instead of the vendored build.

Put provider secrets in the configured env file, for example `.agent-memory/store/.env`. Do not commit that file.

### LLM endpoints

`[llm]` describes the primary judge/reranker endpoint; each `[llm.fallback]`
adds another, tried in order when the one before it fails. Every endpoint is
OpenAI-compatible and resolves its own credential — `api_key_env` for an
environment variable, or `api_key_json` + `api_key_json_path` to read a key
out of another tool's auth store at call time (nothing is copied into this
repo). `extra_body` is merged into every request to that endpoint, which is
where provider pinning and routing hints belong:

```toml
[llm]
model = "kimi-for-coding"
base_url = "http://relay.internal:3000/v1"
api_key_json = "~/.local/share/opencode/auth.json"
api_key_json_path = "huawei.key"

[llm.fallback]
model = "deepseek/deepseek-v4-flash"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"

[llm.fallback.extra_body.provider]
only = ["deepseek"]
```

A fallback must declare its own credential; inheriting the primary's would
make it fail for the same reason the primary did.

An endpoint falls through on transport failure *and* on an empty answer, so a
reasoning model that spends its whole token budget thinking is re-run on the
next endpoint rather than surfacing as a judge failure.

## Tests

The daemon and CLI safety behavior is covered by standard-library `unittest`
tests, so no test dependency is required:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

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

### Writer attribution

`agent_id` is the harness that wrote the memory (`claude`, `codex`, `opencode`,
or `manual`), never the underlying model, so history stays comparable across
model switches. `run_id`/`session_id` is that harness's session id. Detection
reads hard signals only -- identity env vars, then the `AI_AGENT` tag, then
ancestor executable names -- and never the memory text itself.

Claude Code and Codex export their session id to child processes on their own.
opencode does not, so `.opencode/plugin/mem0-local.js` injects
`OPENCODE_SESSION_ID` (and `OPENCODE_CALL_ID`) into every shell command through
opencode's `shell.env` plugin hook. Without the plugin, opencode writes are
still attributed via ancestor-process detection, but carry no session id.
Plugins load at opencode startup: restart a long-lived `opencode serve` after
installing or editing it.

## Optional Daemon

The CLI can use an optional local daemon to avoid paying the Mem0/FastEmbed/ONNX
cold-start cost for every command. The daemon is a user-local Python process
that listens on a Unix socket under the configured store directory; it does not
open a TCP port.

```bash
mem0-local daemon start
mem0-local daemon status
mem0-local daemon stop
```

When the daemon is running, `add`, `search`, `list`, `get`, `update`, `delete`,
and `history` automatically use it. If the daemon is not running, commands fall
back to the direct one-shot CLI path. To force the direct path for debugging,
stop the daemon first and then run:

```bash
MEM0_LOCAL_NO_DAEMON=1 mem0-local search "semantic query"
```

## Configuration

The CLI locates configuration in this order:

1. `MEM0_LOCAL_CONFIG`
2. `.agent-memory/config.toml` found from the current directory upward
3. `~/.config/mem0-local/config.toml`

See `examples/config.toml` for a portable template.

By default vectors live in the embedded Qdrant local-path store. Embedded mode
does not scale past ~20k points (brute-force search, single-process file lock,
no payload indexes); for larger stores run a real Qdrant server and point the
CLI at it:

```toml
[vector_store]
host = "127.0.0.1"
port = 6333
```

`examples/qdrant-server/` holds a `qdrantctl.sh` control script and a config
template for running the official Qdrant binary next to the store. When
`[vector_store]` is set, that server must be running before any memory command.

Runtime data stays under `.agent-memory/store/` and remains excluded from git.

## Audit Manifests

Live mutations append external audit rows under the configured manifest
directory, usually:

```text
.agent-memory/manifests/live-YYYY-MM.jsonl
```

`add`, `update`, `delete`, and `delete --all --force` write one JSONL row per
operation. Each row records the raw CLI input payload, automatic metadata,
scope, Mem0 result, memory ids/result memories when available, timings, and a
payload hash. These manifests are intended to be git-managed human audit logs.

Historical ledger imports and metadata backfills use the same manifest
directory with separate file names such as `ledger-YYYY-MM.jsonl` and
`metadata-backfill-*.jsonl`. Ledger imports use one synthetic OpenCode
writer/session scope while retaining their ledger provenance fields; audited
identity migrations use `ledger-identity-migration-*.jsonl`.

## Agent Integration

Agent-facing operating guidance is deployment-owned and intentionally not
bundled in this repository. A workspace can keep its memory skill under its
own agent configuration (for example, `.agents/skills/local-memory/`) so local
policies, handoff rules, and higher-level retrieval layers can evolve without
coupling them to the reusable `mem0-local` CLI package.
