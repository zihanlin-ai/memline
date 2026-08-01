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
    LOCK_FILE,
    MEM0_DIR,
    MEM0_HOME,
    QDRANT_DIR,
    STORE_DIR,
    llm_endpoint_specs,
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


def require_llm_api_key(job: str = "infer") -> None:
    """Fail fast when ``job``'s primary endpoint has no credential.

    Only the primary is required: a missing fallback credential should not
    block work that the primary can do, and it surfaces loudly enough as the
    second error when the primary is the one that is down.
    """
    from mem0_local.llm import Endpoint

    primary = Endpoint(**llm_endpoint_specs(job)[0])
    try:
        primary.api_key()
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc} Export it or put it in the configured env file ({ENV_FILE})."
        ) from exc


# Budgets, which the code is allowed to know. Which model spends them is the
# configuration's business, and neither number below implies an endpoint.
CLIENT_LLM_MAX_TOKENS = 2000
# The reranker asks for a bare relevance score, but a thinking primary spends
# tokens before it answers (one model used 76 of 100 on a short pair), and a
# truncated score comes back as empty content. 256 keeps the headroom without
# meaningfully changing the cost of an opt-in --rerank search.
RERANKER_MAX_TOKENS = 256


def _placeholder_llm(job: str, max_tokens: int) -> dict[str, Any]:
    """The config mem0 validates before ``install_llm`` replaces the client.

    mem0 checks ``llm.provider`` against a closed list and builds its own
    client, so it has to be handed *something* well-formed. It is handed this
    job's real primary rather than an invented one: if the two disagreed, a
    construction-time error would name an endpoint that never runs a call.
    """
    primary = llm_endpoint_specs(job)[0]
    return {
        # mem0's client type, i.e. "speaks the OpenAI wire protocol" — not a
        # choice of vendor. Every endpoint this package configures is reached
        # this way, whoever serves it.
        "provider": "openai",
        "config": {
            "model": primary["model"],
            "openai_base_url": primary["base_url"],
            "site_url": primary.get("site_url"),
            "app_name": primary.get("app_name"),
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "top_p": 0.1,
            "is_reasoning_model": False,
        },
    }


def build_config() -> dict[str, Any]:
    return {
        "vector_store": vector_store_config(),
        "embedder": {
            "provider": EMBEDDING_PROVIDER,
            "config": {
                "model": EMBEDDING_MODEL,
                "embedding_dims": EMBEDDING_DIMS,
            },
        },
        "llm": _placeholder_llm("infer", CLIENT_LLM_MAX_TOKENS),
        "reranker": {
            "provider": "llm_reranker",
            "config": {
                "top_k": 8,
                "temperature": 0.0,
                "max_tokens": RERANKER_MAX_TOKENS,
                "llm": _placeholder_llm("rerank", RERANKER_MAX_TOKENS),
            },
        },
        "history_db_path": str(HISTORY_DB),
    }


def install_llm(client: Any) -> Any:
    """Replace mem0's own clients with each job's configured endpoint chain.

    mem0 builds one OpenAILLM per consumer from the config dict, and that
    constructor cannot express "try this endpoint, then that one". Swapping the
    objects afterwards is the whole integration — and it is also where the two
    consumers stop sharing a model: extraction and reranking have different
    shapes and different budgets, so they read different tables.
    """
    from mem0_local.llm import build_llm

    client.llm = build_llm(CLIENT_LLM_MAX_TOKENS, job="infer")
    reranker = getattr(client, "reranker", None)
    if getattr(reranker, "llm", None) is not None:
        reranker.llm = build_llm(RERANKER_MAX_TOKENS, job="rerank")
    return client


def check_vendored_mem0() -> None:
    """Fail fast when the official PyPI mem0ai shadows the vendored build.

    This package depends on workspace modifications that only exist in
    ``vendor/mem0ai`` (version ``2.0.x+workspace.N``); the official package
    imports fine but breaks at runtime in non-obvious ways.
    """
    try:
        from importlib.metadata import version

        installed = version("mem0ai")
    except Exception:  # noqa: BLE001 - metadata missing: let the import decide.
        return
    if "workspace" not in installed:
        raise RuntimeError(
            f"mem0ai {installed} is the official package, but mem0-local requires "
            "the vendored build. Install it first: pip install -e <repo>/vendor/mem0ai"
        )


def new_memory_client() -> Any:
    setup_env()
    acquire_cli_lock()
    check_vendored_mem0()
    from mem0 import Memory

    return install_llm(Memory.from_config(build_config()))


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
