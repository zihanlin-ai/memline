"""Shared bootstrap for every mem0-local entry point.

The CLI's direct path, the daemon, and the batch tools (ledger ingest,
metadata backfill) all prepare the environment and build their Mem0 client
here, so the entry points cannot drift apart on env setup, locking, or
client configuration. Presentation concerns (typer/rich, daemon routing)
stay out of this module.
"""

from __future__ import annotations

import os
import warnings
from typing import Any

from mem0_local.config import (
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    ENV_FILE,
    FASTEMBED_CACHE,
    HISTORY_DB,
    LLM_API_KEY_ENV,
    LLM_APP_NAME,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_SITE_URL,
    LOCK_FILE,
    MEM0_DIR,
    MEM0_HOME,
    QDRANT_DIR,
    STORE_DIR,
    vector_store_config,
)

_lock_handle = None
_client: Any = None


def setup_env() -> None:
    warnings.filterwarnings("ignore", message="Payload indexes have no effect in the local Qdrant.*")

    for path in (QDRANT_DIR, MEM0_DIR, MEM0_HOME, FASTEMBED_CACHE):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HOME", str(MEM0_HOME))
    os.environ.setdefault("MEM0_DIR", str(MEM0_DIR))
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(FASTEMBED_CACHE))
    os.environ.setdefault("MEM0_TELEMETRY", "False")

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


def acquire_cli_lock() -> None:
    """Serialize local Qdrant path access across processes."""
    global _lock_handle
    if _lock_handle is not None:
        return
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    _lock_handle = LOCK_FILE.open("a+")
    try:
        import fcntl

        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        return


def require_llm_api_key() -> None:
    if not os.environ.get(LLM_API_KEY_ENV):
        raise RuntimeError(
            f"{LLM_API_KEY_ENV} is not set. Export it or put it in the configured env file."
        )


def build_config() -> dict[str, Any]:
    openrouter_llm = {
        "provider": "openai",
        "config": {
            "model": LLM_MODEL,
            "openrouter_base_url": LLM_BASE_URL,
            "site_url": LLM_SITE_URL,
            "app_name": LLM_APP_NAME,
            "temperature": 0.0,
            "max_tokens": 2000,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    }

    return {
        "vector_store": vector_store_config(),
        "embedder": {
            "provider": EMBEDDING_PROVIDER,
            "config": {
                "model": EMBEDDING_MODEL,
                "embedding_dims": EMBEDDING_DIMS,
            },
        },
        "llm": openrouter_llm,
        "reranker": {
            "provider": "llm_reranker",
            "config": {
                "top_k": 8,
                "temperature": 0.0,
                "max_tokens": 100,
                "llm": openrouter_llm,
            },
        },
        "history_db_path": str(HISTORY_DB),
    }


def new_memory_client() -> Any:
    setup_env()
    acquire_cli_lock()
    from mem0 import Memory

    return Memory.from_config(build_config())


def get_client() -> Any:
    """Process-cached client for repeated in-process ops (CLI direct path)."""
    global _client
    if _client is None:
        _client = new_memory_client()
    return _client


def normalize_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "memories"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []
