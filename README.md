# memline

`memline` (formerly `mem0-local`) is a local-first Mem0 CLI wrapper. It keeps
runtime state under a configured local store, backs vectors with Qdrant
(embedded local path mode by default, or a local Qdrant server via
`[vector_store]`), and writes audit metadata for timestamps, writer identity,
sessions, schema version, and updates. On top of the store it ships the
deterministic programs of an LLM wiki pipeline (`memline wiki ...`).

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
- Agent discovery: the `skills/local-memory/` skill and the launchers under
  `integrations/` that tell Codex/Claude/opencode to call `memline`.

Only the package and workspace profile are meant to be committed. Runtime store contents stay local.

## Bootstrap a new workspace

Everything a fresh workspace needs, in order. `<ws>` is the workspace root.

```bash
# 0. The checkout. The standard location is inside the workspace:
git clone https://github.com/linzihan-tech/memline.git <ws>/.agent-memory/projects/memline

# 1. The store venv. Vendored mem0ai FIRST -- the official PyPI mem0ai will
#    not work (this package depends on the modifications in vendor/mem0ai).
python -m venv <ws>/.agent-memory/store/venv
<ws>/.agent-memory/store/venv/bin/pip install -e <ws>/.agent-memory/projects/memline/vendor/mem0ai
<ws>/.agent-memory/store/venv/bin/pip install -e <ws>/.agent-memory/projects/memline

# 2. The workspace profile. Copy the template and replace every path.
cp <ws>/.agent-memory/projects/memline/examples/config.toml <ws>/.agent-memory/config.toml

# 3. Secrets. Put provider keys in the configured env file; never commit it.
touch <ws>/.agent-memory/store/.env

# 4. The launcher, on PATH.
mkdir -p <ws>/.agent-memory/bin
ln -s ../projects/memline/integrations/bin/memline <ws>/.agent-memory/bin/memline
ln -s <ws>/.agent-memory/bin/memline ~/.local/bin/memline

# 5. Agent integration (each is optional):
#    - skill: symlink skills/local-memory into the workspace agent config,
#      e.g. <ws>/.agents/skills/local-memory -> ../../.agent-memory/projects/memline/skills/local-memory
#    - opencode session attribution:
mkdir -p <ws>/.opencode/plugin
ln -s ../../.agent-memory/projects/memline/integrations/opencode/memline.js <ws>/.opencode/plugin/memline.js

# 6. Larger stores: run a local qdrant server (see examples/qdrant-server/)
#    and add [vector_store] to config.toml.

# 7. Verify.
memline status
```

When the checkout lives elsewhere, or without the launcher, set
`MEMLINE_CONFIG=<ws>/.agent-memory/config.toml` and run
`python -m memline.cli` from the venv directly.

For development from a checkout:

```bash
.agent-memory/store/venv/bin/pip install -e ./vendor/mem0ai -e .
```

The CLI fails fast with a clear error if the official mem0ai package is
installed instead of the vendored build.

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
api_key_json_path = "provider.key"

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

#### Endpoints behind a proxy

A `base_url` on plain HTTP is reached through a CONNECT tunnel rather than
being forward-proxied, because a forward proxy rewrites the request and may
drop the body — which reads as `400 invalid JSON request body` from the
endpoint on *every* call, and quietly demotes the whole pipeline to the
fallback. `https://` endpoints are untouched: they are already tunnelled. A
host that the environment's `no_proxy` exempts (including glob entries such
as `10.*`, which httpx itself does not honour) is reached directly.

`stream = true` on an endpoint asks for the answer as a token stream and
reassembles it before returning; the answer is identical either way. Set it
when the network path between here and the endpoint gives up on a slow first
response byte — a judge at a few thousand `max_tokens` can easily take longer
than such a limit, and a stream starts emitting immediately.

## Tests

The daemon and CLI safety behavior is covered by standard-library `unittest`
tests, so no test dependency is required:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Commands

```bash
memline status
memline add "accurate atomic memory text"
memline search "semantic query"
memline list --filter agent_id=codex
memline get <memory_id>
memline update <memory_id> "corrected memory text"
memline delete <memory_id>
```

Routine agents should call `add` with only the memory text. The CLI auto-detects agent/session context when possible, writes timestamps and schema metadata, and returns JSON in agent contexts.

Raw adds reject non-Latin letters before any store or audit mutation. Rewrite
the narration in English, or use `memline add "..." --force` only when the
non-Latin content must be preserved. This override applies only to the language
gate; the raw-write length cap remains absolute. Extraction input is checked
after storage by the existing correctness judge rather than at the source-text
boundary.

Use `list --filter ...` for structured audits by metadata fields such as `agent_id`, `run_id`, `source`, `session_id`, `created_at`, or `ingested_at`. Keep `search` for semantic retrieval.

### Writer attribution

`agent_id` is the harness that wrote the memory (`claude`, `codex`, `opencode`,
or `manual`), never the underlying model, so history stays comparable across
model switches. `run_id`/`session_id` is that harness's session id. Detection
reads hard signals only -- identity env vars, then the `AI_AGENT` tag, then
ancestor executable names -- and never the memory text itself.

Claude Code and Codex export their session id to child processes on their own.
opencode does not, so `integrations/opencode/memline.js` (installed at
`.opencode/plugin/memline.js`) injects `OPENCODE_SESSION_ID` (and
`OPENCODE_CALL_ID`) into every shell command through opencode's `shell.env`
plugin hook. Without the plugin, opencode writes are still attributed via
ancestor-process detection, but carry no session id. Plugins load at opencode
startup: restart a long-lived `opencode serve` after installing or editing it.

## Optional Daemon

The CLI can use an optional local daemon to avoid paying the Mem0/FastEmbed/ONNX
cold-start cost for every command. The daemon is a user-local Python process
that listens on a Unix socket under the configured store directory; it does not
open a TCP port.

```bash
memline daemon start
memline daemon status
memline daemon stop
```

When the daemon is running, `add`, `search`, `list`, `get`, `update`, `delete`,
and `history` automatically use it. If the daemon is not running, commands fall
back to the direct one-shot CLI path. To force the direct path for debugging,
stop the daemon first and then run:

```bash
MEMLINE_NO_DAEMON=1 memline search "semantic query"
```

## Configuration

The CLI locates configuration in this order:

1. `MEMLINE_CONFIG`
2. `.agent-memory/config.toml` found from the current directory upward
3. `~/.config/memline/config.toml`

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

### Deployment facts the code refuses to guess

Three things are facts about one deployment and have no default in code:

```toml
[memory]
local_tz_offset_hours = 8      # naive-timestamp timezone; omit = system local

[sanitize]                     # REQUIRED before any outbound wiki flow
internal_domains = ["corp.example.com"]
internal_repo_hosts = ["git.corp.example.com"]

[wiki]
domain = """What this workspace works on, for the profiling prompts."""
```

`[sanitize]` fails closed: with the keys absent, `wiki bundle`, drafting and
the draft leak check refuse to run rather than let internal hostnames leave
the machine on a guess. An explicit empty list is a deliberate declaration
that the deployment has none. The generic shape rules (IP addresses, account
ids, job ids) are built in and always active.

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

Agent-facing operating guidance ships in this repository at
`skills/local-memory/` — the skill that teaches an agent the memory
discipline (atomic raw writes, retrieval habits, lifecycle hygiene, the
handoff review) and the wiki pipeline's gates. A workspace installs it by
symlinking the directory into its agent configuration (see Bootstrap step 5),
so the live skill and the committed one are the same files.

The skill states policy in deployment-neutral terms; facts specific to one
deployment (hosts, endpoints, project names) belong in that workspace's
memory store and configuration, not in the skill text.
