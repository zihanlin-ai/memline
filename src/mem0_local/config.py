"""Configuration loading for mem0-local."""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


LOCAL_TZ_OFFSET_HOURS = 8
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def find_config_path() -> Path | None:
    env_path = os.environ.get("MEM0_LOCAL_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for current in (cwd, *cwd.parents):
        candidate = current / ".agent-memory" / "config.toml"
        if candidate.exists():
            return candidate

    user_config = Path.home() / ".config" / "mem0-local" / "config.toml"
    if user_config.exists():
        return user_config
    return None


CONFIG_PATH = find_config_path()
CONFIG = _load_toml(CONFIG_PATH)


def section(name: str) -> dict[str, Any]:
    value = CONFIG.get(name, {})
    return value if isinstance(value, dict) else {}


def value(section_name: str, key: str, default: Any) -> Any:
    return section(section_name).get(key, default)


def path_value(section_name: str, key: str, default: Path | str) -> Path:
    raw = value(section_name, key, default)
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path
    base = CONFIG_PATH.parent if CONFIG_PATH else Path.cwd()
    return (base / path).resolve()


def default_workspace_root() -> Path:
    if CONFIG_PATH and CONFIG_PATH.parent.name == ".agent-memory":
        return CONFIG_PATH.parent.parent.resolve()
    return Path.cwd().resolve()


WORKSPACE_ROOT = path_value("memory", "workspace_root", default_workspace_root())
MEMORY_ROOT = path_value("memory", "memory_root", WORKSPACE_ROOT / ".agent-memory")
STORE_DIR = path_value("memory", "store_dir", MEMORY_ROOT / "store")
QDRANT_DIR = path_value("paths", "qdrant_dir", STORE_DIR / "qdrant")
MEM0_DIR = path_value("paths", "mem0_dir", STORE_DIR / "mem0")
MEM0_HOME = path_value("paths", "home_dir", STORE_DIR / "home")
FASTEMBED_CACHE = path_value("paths", "fastembed_cache", STORE_DIR / "model-cache" / "fastembed")
HISTORY_DB = path_value("paths", "history_db", STORE_DIR / "history.db")
SESSION_STATS_DB = path_value("paths", "session_stats_db", STORE_DIR / "session-stats.db")
MANIFEST_DIR = path_value("paths", "manifest_dir", MEMORY_ROOT / "manifests")
MANIFEST_LOCK = path_value("paths", "manifest_lock", STORE_DIR / "manifest.lock")
ENV_FILE = path_value("llm", "env_file", STORE_DIR / ".env")
LOCK_FILE = path_value("paths", "lock_file", STORE_DIR / "cli.lock")

COLLECTION = str(value("memory", "collection", "workspace_agent_memory"))
DEFAULT_USER_ID = str(value("memory", "user_id", "workspace"))
MEMORY_SCHEMA_VERSION = int(value("memory", "schema_version", 2))
# Hard cap on raw (verbatim) add/update text length, in characters. Raw entries
# must be atomic single facts; longer content has to be split into multiple
# adds or routed through --infer extraction. Default sits at ~p95 of the
# existing store (p50≈320, p95≈630): anything longer is a multi-fact dump.
MAX_RAW_TEXT_CHARS = int(value("memory", "max_raw_text_chars", 600))
# Once a session has accumulated this many live adds, every CLI invocation
# from it prints an advisory handoff banner (consider a handoff review and
# telling the user). 0 disables the banner.
SESSION_ADD_ALERT_THRESHOLD = int(value("memory", "session_add_alert_threshold", 200))

EMBEDDING_PROVIDER = str(value("embedder", "provider", "fastembed"))
EMBEDDING_MODEL = str(value("embedder", "model", "jinaai/jina-embeddings-v2-base-zh"))
EMBEDDING_DIMS = int(value("embedder", "dims", 768))

# ---------------------------------------------------------------------------
# LLM endpoints
#
# Every LLM call in this package belongs to a named *job*, and each job reads
# its endpoint from ``[llm.<job>]``. The split of responsibility is deliberate
# and total: the code owns the job names and the knobs (budgets, retries,
# timeouts), the configuration owns every identity — vendor, model, base_url,
# credential. None of those has a default here.
#
# That rule was bought. The relay serving the drafting model ran out of quota
# mid-run and its channel then disappeared entirely, and because this module
# could still resolve *an* endpoint from built-in defaults, the failure never
# surfaced as "your configuration names a model that no longer exists" — work
# just continued somewhere else, on someone's metered account, producing
# articles nobody could tell apart until they were measured. An endpoint
# identity nobody wrote down is not a default; it is a guess about which
# vendor gets billed.
#
# Jobs are split by the work, not by the module that happens to call them, so
# that two jobs with genuinely different economics can never be forced onto one
# model. The pairing that matters most is draft/review: an audit is only
# independent if it is allowed to run on a different model from the writing it
# audits, and that is a configuration question, not a code one.
LLM_JOBS = (
    "infer",    # mem0's own fact extraction behind `add --infer`
    "rerank",   # the opt-in `search --rerank` relevance scorer
    "judge",    # staleness / necessity / safety / correctness
    "profile",  # wiki: one call per memory batch or source document
    "draft",    # wiki: one call per article
    "review",   # wiki: the independent audit of a draft
)


class ConfigError(RuntimeError):
    """config.toml cannot answer a question the code must not answer for it."""


# Fields of one endpoint spec, mirroring mem0_local.llm.Endpoint. Anything
# else under [llm] (paths, provider name, per-job knobs) is config for other
# layers and must not reach the Endpoint constructor.
_ENDPOINT_FIELDS = (
    "model",
    "base_url",
    "api_key_env",
    "api_key_json",
    "api_key_json_path",
    "site_url",
    "app_name",
    "stream",
    "extra_body",
)

# Knobs a job may state in config; anything absent keeps the caller's own
# value. These are budgets and patience, not identities: a wrong one costs a
# retry, where a wrong identity costs the wrong vendor.
_KNOB_FIELDS = ("max_tokens", "attempts_per_endpoint", "timeout", "backoff")


def _endpoint_spec(raw: dict[str, Any], name: str, *, inherit: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": name}
    for key in _ENDPOINT_FIELDS:
        if key in raw:
            spec[key] = raw[key]
        elif key in inherit:
            spec[key] = inherit[key]
    # A fallback that silently reuses the primary's credential is not a
    # fallback; require it to name its own, and never inherit one.
    if name != "primary" and "api_key_env" not in raw and "api_key_json" not in raw:
        raise ValueError(f"[llm.{name}] must declare api_key_env or api_key_json")
    return spec


def _require_identity(spec: dict[str, Any], where: str) -> dict[str, Any]:
    """Fail with the exact missing key rather than resolving to a built-in one."""
    missing = [key for key in ("model", "base_url") if not spec.get(key)]
    if not (spec.get("api_key_env") or spec.get("api_key_json")):
        missing.append("api_key_env or api_key_json")
    if not missing:
        return spec
    # A fallback states its whole identity; only a job table inherits. Saying
    # otherwise would send someone to edit [llm] and watch nothing change.
    fix = ("state it on this table" if spec.get("name") != "primary"
           else "state it on this table or on [llm] for it to inherit")
    raise ConfigError(
        f"[{where}] is missing {', '.join(missing)}. Endpoint identity has no "
        f"default in code — {fix}, in {CONFIG_PATH or 'config.toml'}.")


def _resolve_job_table(job: str | None) -> dict[str, Any]:
    """``[llm]`` merged with ``[llm.<job>]``, with the job's own keys winning."""
    llm = section("llm")
    if job is None:
        return llm
    if job not in LLM_JOBS:
        # A typo must not resolve to some other job's model. The old behaviour
        # here was to fall through to [llm] so a caller was never disabled;
        # that traded a loud failure for a silent bill on the wrong endpoint.
        raise ConfigError(f"unknown llm job {job!r}; known jobs: {', '.join(LLM_JOBS)}")
    chosen = llm.get(job)
    if not isinstance(chosen, dict):
        return llm
    # A job inherits what it does not state — base_url, credential, attribution
    # headers, extra_body — so a job may name a model and nothing else. Two
    # things never cross the boundary:
    #
    # ``fallback``, because a fallback is a choice of *which other model* runs
    # this job when the first one cannot, and the parent's answer was chosen
    # for different work. Inheriting it is how a drafting job silently acquires
    # the judges' cheap vendor and returns a thinner article that still looks
    # like a success. A job with no fallback of its own has no fallback.
    #
    # Sibling job tables, because they are other jobs, not settings for this
    # one.
    #
    # A key the job does state replaces the inherited one whole: a half-merged
    # reasoning config is worse than either half of it.
    siblings = {k for k, v in llm.items()
                if isinstance(v, dict) and k not in ("fallback", "extra_body")}
    inherited = {k: v for k, v in llm.items()
                 if k not in chosen and k not in siblings and k != "fallback"}
    return {**inherited, **chosen}


def llm_knobs(job: str | None = None) -> dict[str, Any]:
    """Budgets and patience this job states, if any. Absent means "caller decides"."""
    table = _resolve_job_table(job)
    return {key: table[key] for key in _KNOB_FIELDS if key in table}


def llm_endpoint_specs(job: str | None = None) -> list[dict[str, Any]]:
    """Endpoints in preference order: the job's own table first, then its fallbacks.

    ``[llm.fallback]`` may be a single table or an array of tables; presenting
    both as one ordered list keeps the retry loop indifferent to how many
    fallbacks exist.

    ``job`` selects a table — ``[llm.draft]`` for ``job="draft"`` — so work with
    different economics never shares a model by accident. Anything the job
    omits is inherited from ``[llm]``, so a job may name a model and nothing
    else; what it may not do is leave the identity unnamed everywhere, because
    there is nothing here to fall back on.
    """
    llm = _resolve_job_table(job)
    primary = _require_identity(_endpoint_spec(llm, "primary", inherit={}),
                                f"llm.{job}" if job else "llm")

    raw_fallbacks = llm.get("fallback") or []
    if isinstance(raw_fallbacks, dict):
        raw_fallbacks = [raw_fallbacks]
    # site_url/app_name are attribution headers, not credentials: inheriting
    # them keeps every endpoint reporting the same caller identity.
    inherit = {k: primary[k] for k in ("site_url", "app_name") if k in primary}
    specs = [primary]
    for index, raw in enumerate(raw_fallbacks):
        name = "fallback" if len(raw_fallbacks) == 1 else f"fallback{index + 1}"
        where = f"llm.{job}.{name}" if job else f"llm.{name}"
        specs.append(_require_identity(_endpoint_spec(raw, name, inherit=inherit), where))
    return specs

MANUAL_SOURCE = str(value("metadata", "manual_source", "manual"))
MANUAL_SESSION = str(value("metadata", "manual_session", "manual-session"))
# Imported ledgers share one synthetic OpenCode writer/session identity. Their
# month/date/file remain separate provenance fields rather than scope IDs.
LEDGER_IMPORT_AGENT_ID = str(value("metadata", "ledger_import_agent_id", "opencode"))
LEDGER_IMPORT_SESSION_ID = str(
    value("metadata", "ledger_import_session_id", "ses_b8d2ac181351976b11df6be5bb")
)

# Optional qdrant server mode: set [vector_store] host/port to use a running
# qdrant server instead of the embedded local-path store.
VECTOR_STORE_HOST = value("vector_store", "host", None)
VECTOR_STORE_PORT = value("vector_store", "port", None)
VECTOR_STORE_MODE = (
    "qdrant-server" if VECTOR_STORE_HOST and VECTOR_STORE_PORT else "qdrant-local-path"
)


def vector_store_config() -> dict[str, Any]:
    """Mem0 vector_store block for the active mode (server or local path)."""
    config: dict[str, Any] = {
        "collection_name": COLLECTION,
        "embedding_model_dims": EMBEDDING_DIMS,
        "on_disk": True,
    }
    if VECTOR_STORE_MODE == "qdrant-server":
        config["host"] = str(VECTOR_STORE_HOST)
        config["port"] = int(VECTOR_STORE_PORT)
    else:
        config["path"] = str(QDRANT_DIR)
    return {"provider": "qdrant", "config": config}
