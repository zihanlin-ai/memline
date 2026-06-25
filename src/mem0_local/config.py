"""Configuration loading for mem0-local."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


LOCAL_TZ_OFFSET_HOURS = 8


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
ENV_FILE = path_value("llm", "env_file", STORE_DIR / ".env")
LOCK_FILE = path_value("paths", "lock_file", STORE_DIR / "cli.lock")

COLLECTION = str(value("memory", "collection", "workspace_agent_memory"))
DEFAULT_USER_ID = str(value("memory", "user_id", "workspace"))
MEMORY_SCHEMA_VERSION = int(value("memory", "schema_version", 2))

EMBEDDING_PROVIDER = str(value("embedder", "provider", "fastembed"))
EMBEDDING_MODEL = str(value("embedder", "model", "jinaai/jina-embeddings-v2-base-zh"))
EMBEDDING_DIMS = int(value("embedder", "dims", 768))

LLM_PROVIDER = str(value("llm", "provider", "openrouter"))
LLM_MODEL = str(value("llm", "model", "@preset/work"))
LLM_BASE_URL = str(value("llm", "base_url", "https://openrouter.ai/api/v1"))
LLM_SITE_URL = str(value("llm", "site_url", "http://localhost"))
LLM_APP_NAME = str(value("llm", "app_name", "mem0-local"))
LLM_API_KEY_ENV = str(value("llm", "api_key_env", "OPENROUTER_API_KEY"))

MANUAL_SOURCE = str(value("metadata", "manual_source", "manual"))
MANUAL_SESSION = str(value("metadata", "manual_session", "manual-session"))

