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

LLM_PROVIDER = str(value("llm", "provider", "openrouter"))
LLM_MODEL = str(value("llm", "model", "@preset/work"))
LLM_BASE_URL = str(value("llm", "base_url", "https://openrouter.ai/api/v1"))
LLM_SITE_URL = str(value("llm", "site_url", "http://localhost"))
LLM_APP_NAME = str(value("llm", "app_name", "mem0-local"))
LLM_API_KEY_ENV = str(value("llm", "api_key_env", "OPENROUTER_API_KEY"))

# Fields of one endpoint spec, mirroring mem0_local.llm.Endpoint. Anything
# else under [llm] (paths, provider name, the legacy keys above) is config
# for other layers and must not reach the Endpoint constructor.
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


def llm_endpoint_specs() -> list[dict[str, Any]]:
    """Judge endpoints in preference order: [llm] first, then [llm.fallback].

    ``[llm.fallback]`` may be a single table or an array of tables; presenting
    both as one ordered list keeps the retry loop indifferent to how many
    fallbacks exist.
    """
    llm = section("llm")
    primary = _endpoint_spec(llm, "primary", inherit={})
    primary.setdefault("model", LLM_MODEL)
    primary.setdefault("base_url", LLM_BASE_URL)
    # The legacy api_key_env default only applies when the primary names no
    # credential at all. Defaulting it unconditionally would hand an endpoint
    # that reads its key from a file the *other* endpoint's env credential,
    # because api_key_env is consulted first.
    if "api_key_env" not in primary and "api_key_json" not in primary:
        primary["api_key_env"] = LLM_API_KEY_ENV

    raw_fallbacks = llm.get("fallback") or []
    if isinstance(raw_fallbacks, dict):
        raw_fallbacks = [raw_fallbacks]
    # site_url/app_name are attribution headers, not credentials: inheriting
    # them keeps every endpoint reporting the same caller identity.
    inherit = {k: primary[k] for k in ("site_url", "app_name") if k in primary}
    specs = [primary]
    for index, raw in enumerate(raw_fallbacks):
        name = "fallback" if len(raw_fallbacks) == 1 else f"fallback{index + 1}"
        specs.append(_endpoint_spec(raw, name, inherit=inherit))
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
