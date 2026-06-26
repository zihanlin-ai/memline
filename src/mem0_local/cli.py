#!/usr/bin/env python3
"""Local mem0-compatible CLI for the workspace memory store."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer
from rich.console import Console
from rich.table import Table

from mem0_local.audit import append_live_audit
from mem0_local.config import (
    COLLECTION,
    CONFIG_PATH,
    DEFAULT_USER_ID,
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
    LLM_PROVIDER,
    LLM_SITE_URL,
    LOCK_FILE,
    MANUAL_SESSION,
    MANUAL_SOURCE,
    MEMORY_ROOT,
    MEMORY_SCHEMA_VERSION,
    MEM0_DIR,
    MEM0_HOME,
    QDRANT_DIR,
    STORE_DIR,
    WORKSPACE_ROOT,
)

ROOT = WORKSPACE_ROOT
LOCAL_TZ = timezone(timedelta(hours=8))

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="mem0-local",
    help="Local mem0-compatible CLI backed by .agent-memory/store",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
daemon_app = typer.Typer(help="Manage the optional long-lived local daemon.")
app.add_typer(daemon_app, name="daemon")

agent_mode = False
lock_handle = None


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
    """Serialize local Qdrant path access across CLI processes."""
    global lock_handle
    if lock_handle is not None:
        return
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("a+")
    try:
        import fcntl

        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except ImportError:
        return


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
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": COLLECTION,
                "path": str(QDRANT_DIR),
                "embedding_model_dims": EMBEDDING_DIMS,
                "on_disk": True,
            },
        },
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


def memory_client():
    setup_env()
    if os.environ.get("MEM0_LOCAL_NO_DAEMON", "").lower() in {"1", "true", "yes", "on"}:
        try:
            from mem0_local.daemon import status as daemon_status

            if daemon_status().get("running"):
                raise click.ClickException("mem0-local daemon is running; stop it before using the direct path")
        except click.ClickException:
            raise
        except Exception:
            pass
    acquire_cli_lock()
    if not os.environ.get(LLM_API_KEY_ENV):
        raise typer.BadParameter(
            f"{LLM_API_KEY_ENV} is not set. Export it or put it in the configured env file."
        )

    from mem0 import Memory

    return Memory.from_config(build_config())


def output(data: Any, *, command: str, fmt: str = "text", scope: dict[str, str] | None = None) -> None:
    if fmt in {"json", "agent"} or agent_mode:
        envelope = {"status": "success", "command": command, "data": data}
        if scope:
            envelope["scope"] = scope
        if isinstance(data, list):
            envelope["count"] = len(data)
        console.print_json(json.dumps(envelope if (fmt == "agent" or agent_mode) else data, default=str))
        return
    if fmt == "quiet":
        return
    render_text(command, data)


def daemon_enabled() -> bool:
    value = os.environ.get("MEM0_LOCAL_NO_DAEMON", "")
    return value.lower() not in {"1", "true", "yes", "on"}


def maybe_daemon_request(op: str, args: dict[str, Any]) -> tuple[bool, Any]:
    if not daemon_enabled():
        return False, None
    try:
        from mem0_local.daemon import DaemonUnavailable, PID_PATH, SOCKET_PATH, request
    except Exception:
        return False, None
    try:
        return True, request({"op": op, "args": args}, timeout=daemon_operation_timeout(op, args))
    except DaemonUnavailable as exc:
        if SOCKET_PATH.exists() or PID_PATH.exists():
            raise click.ClickException(
                "mem0-local daemon appears to be configured but is not reachable "
                f"({exc}). Run `mem0-local daemon status`; if it is stale, run "
                "`mem0-local daemon stop` and retry, or set MEM0_LOCAL_NO_DAEMON=1 "
                "for the direct path."
            ) from exc
        return False, None
    except Exception as exc:
        raise click.ClickException(f"mem0-local daemon {op} failed: {exc}") from exc


def daemon_operation_timeout(op: str, args: dict[str, Any]) -> float:
    raw = os.environ.get("MEM0_LOCAL_DAEMON_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if op == "search":
        return 180.0 if args.get("rerank") else 30.0
    if op == "add":
        return 300.0 if args.get("infer") else 30.0
    if op in {"get", "list", "delete", "history"}:
        return 30.0
    return 300.0


def chosen_format(output_format: str, json_flag: bool) -> str:
    if json_flag:
        return "agent"
    if not output_option_was_passed() and auto_agent_output():
        return "agent"
    return output_format


def output_option_was_passed() -> bool:
    return any(arg == "--output" or arg == "-o" or arg.startswith("--output=") for arg in sys.argv[1:])


def auto_agent_output() -> bool:
    explicit = os.environ.get("MEM0_LOCAL_AUTO_JSON")
    if explicit is not None:
        return explicit.lower() not in {"0", "false", "no", "off"}
    return detect_writer_context().get("source") in {"codex", "claude"}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        raw = f"{raw}T00:00:00+08:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"Timestamp must be ISO-8601, got: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.isoformat()


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def read_proc_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode(errors="replace").strip()


def read_proc_ppid(pid: int) -> int | None:
    try:
        status = (Path("/proc") / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def process_chain_text(limit: int = 10) -> str:
    texts: list[str] = []
    pid = os.getpid()
    for _ in range(limit):
        cmdline = read_proc_cmdline(pid)
        if cmdline:
            texts.append(cmdline)
        ppid = read_proc_ppid(pid)
        if not ppid or ppid <= 1 or ppid == pid:
            break
        pid = ppid
    return "\n".join(texts).lower()


def detect_writer_context() -> dict[str, str]:
    """Best-effort local caller detection for audit metadata."""
    source = first_env("MEM0_LOCAL_SOURCE", "MEM0_SOURCE", "AGENT_SOURCE", "AI_AGENT_SOURCE")
    session_id = first_env(
        "MEM0_LOCAL_SESSION_ID",
        "MEM0_SESSION_ID",
        "AGENT_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE_SESSION_ID",
    )

    process_text = process_chain_text()
    if not source:
        if first_env("CODEX_THREAD_ID", "CODEX_MANAGED_PACKAGE_ROOT") or "codex" in process_text:
            source = "codex"
        elif (
            first_env("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE_SESSION_ID")
            or "claude" in process_text
        ):
            source = "claude"

    context: dict[str, str] = {}
    if source:
        context["source"] = source
    if session_id:
        context["session_id"] = session_id
    return context


def render_text(command: str, data: Any) -> None:
    if command == "status":
        console.print_json(json.dumps(data, default=str))
        return

    if command in {"search", "list"}:
        items = normalize_items(data)
        if not items:
            console.print("No memories found.")
            return
        table = Table("ID", "Score", "Memory", "Created", "Metadata")
        for item in items:
            table.add_row(
                str(item.get("id", ""))[:12],
                format_score(item),
                str(item.get("memory") or item.get("text") or ""),
                str(item.get("created_at") or ""),
                compact_json(item.get("metadata") or {}),
            )
        console.print(table)
        return

    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


def normalize_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "memories"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def format_score(item: dict[str, Any]) -> str:
    score = item.get("score", item.get("rerank_score"))
    if isinstance(score, int | float):
        return f"{score:.3f}"
    return ""


def compact_json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= 80 else text[:77] + "..."


def parse_json_or_key_values(values: list[str] | None, *, option_name: str) -> dict[str, Any]:
    if not values:
        return {}

    if len(values) == 1:
        raw = values[0].strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise typer.BadParameter(f"Invalid JSON for {option_name}: {exc}") from None
            if not isinstance(parsed, dict):
                raise typer.BadParameter(f"{option_name} JSON must be an object.")
            return parsed

    parsed: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(f"{option_name} must be JSON or key=value, got: {value}")
        key, raw = value.split("=", 1)
        parsed[key] = coerce_scalar(raw)
    return parsed


def coerce_scalar(raw: str) -> Any:
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.lower() == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def scope_dict(
    user_id: str | None,
    agent_id: str | None,
    app_id: str | None,
    run_id: str | None,
) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "user_id": user_id,
            "agent_id": agent_id,
            "app_id": app_id,
            "run_id": run_id,
        }.items()
        if value
    }


def filters_from_scope(
    user_id: str | None,
    agent_id: str | None,
    app_id: str | None,
    run_id: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if user_id:
        filters["user_id"] = user_id
    if agent_id:
        filters["agent_id"] = agent_id
    if run_id:
        filters["run_id"] = run_id
    if app_id:
        filters["app_id"] = app_id
    if extra:
        filters.update(extra)
    return filters


def read_content(text: str | None, messages: str | None, file: Path | None) -> Any:
    if file:
        try:
            raw = file.read_text()
        except OSError as exc:
            raise typer.BadParameter(f"Failed to read --file: {exc}") from None
        return parse_messages_or_text(raw)

    if messages:
        return parse_messages_or_text(messages)

    if text:
        return text

    if stdin_is_piped():
        piped = sys.stdin.read().strip()
        if piped:
            return piped

    raise typer.BadParameter("No content provided. Pass text, --messages, --file, or stdin.")


def parse_messages_or_text(raw: str) -> Any:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "messages" in parsed:
        return parsed["messages"]
    return parsed


def stdin_is_piped() -> bool:
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)
    except Exception:
        return False


@app.callback()
def main(
    json_output: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
) -> None:
    global agent_mode
    agent_mode = json_output


@daemon_app.command("start")
def daemon_start(
    wait_seconds: float = typer.Option(90.0, "--wait", help="Seconds to wait for daemon readiness."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Start the optional local daemon."""
    from mem0_local.daemon import start_daemon

    result = start_daemon(wait_seconds=wait_seconds)
    output(result, command="daemon-start", fmt=chosen_format(output_format, json_flag))


@daemon_app.command("stop")
def daemon_stop(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Stop the optional local daemon."""
    from mem0_local.daemon import stop_daemon

    result = stop_daemon()
    output(result, command="daemon-stop", fmt=chosen_format(output_format, json_flag))


@daemon_app.command("status")
def daemon_status(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show optional local daemon status."""
    from mem0_local.daemon import status as daemon_status_data

    result = daemon_status_data()
    output(result, command="daemon-status", fmt=chosen_format(output_format, json_flag))


@app.command()
def status(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show local memory store configuration."""
    setup_env()
    data = {
        "backend": "local",
        "root": str(ROOT),
        "config_path": str(CONFIG_PATH) if CONFIG_PATH else None,
        "collection": COLLECTION,
        "vector_store": "qdrant-local-path",
        "qdrant_path": str(QDRANT_DIR),
        "history_db_path": str(HISTORY_DB),
        "mem0_dir": os.environ["MEM0_DIR"],
        "fastembed_cache_path": os.environ["FASTEMBED_CACHE_PATH"],
        "embedder": {"provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "dims": EMBEDDING_DIMS},
        "llm": {
            "provider": LLM_PROVIDER,
            "model": LLM_MODEL,
            "api_key_env": LLM_API_KEY_ENV,
            "api_key_set": bool(os.environ.get(LLM_API_KEY_ENV)),
            "base_url": LLM_BASE_URL,
        },
        "reranker": {"provider": "llm_reranker", "llm_provider": LLM_PROVIDER, "llm_model": LLM_MODEL},
        "auto_context": detect_writer_context(),
    }
    output(data, command="status", fmt=chosen_format(output_format, json_flag))


@app.command()
def add(
    text: Optional[str] = typer.Argument(None, help="Text content to add."),
    user_id: str = typer.Option(DEFAULT_USER_ID, "--user-id", "-u", help="Scope to user."),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Scope to agent."),
    app_id: Optional[str] = typer.Option(None, "--app-id", help="Stored as metadata for local mode."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Scope to run."),
    messages: Optional[str] = typer.Option(None, "--messages", help="Conversation messages as JSON."),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="Read text/messages from file."),
    metadata: list[str] = typer.Option([], "--metadata", "-m", help="JSON object or key=value."),
    timestamp: Optional[str] = typer.Option(
        None,
        "--timestamp",
        "--created-at",
        help="Original memory timestamp; stored as created_at metadata.",
    ),
    ledger_timestamp: Optional[str] = typer.Option(
        None,
        "--ledger-timestamp",
        help="Original ledger/event timestamp; defaults to --timestamp.",
    ),
    no_infer: bool = typer.Option(False, "--no-infer", help="Store raw text without LLM extraction."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Add a memory from text, messages, file, or stdin."""
    start = time.perf_counter()
    started_at = now_utc_iso()
    content = read_content(text, messages, file)
    meta = parse_json_or_key_values(metadata, option_name="--metadata")
    auto_context = detect_writer_context()
    if auto_context.get("source"):
        meta.setdefault("source", auto_context["source"])
    if auto_context.get("session_id"):
        meta.setdefault("session_id", auto_context["session_id"])
    meta.setdefault("source", MANUAL_SOURCE)
    if agent_id is None:
        agent_id = str(meta.get("source") or MANUAL_SOURCE)
    if run_id is None:
        run_id = str(meta.get("session_id") or MANUAL_SESSION)
    meta.setdefault("session_id", run_id)
    meta.setdefault("writer_agent_id", agent_id)
    meta.setdefault("origin", "ledger_import" if meta.get("source") == "agent-memory-ledger" else "live_agent")
    meta.setdefault("memory_schema_version", MEMORY_SCHEMA_VERSION)
    if app_id:
        meta.setdefault("app_id", app_id)
    ingested_at = now_utc_iso()
    created_at = normalize_timestamp(timestamp) or meta.get("created_at") or ingested_at
    meta["created_at"] = normalize_timestamp(str(created_at))
    meta["ledger_timestamp"] = normalize_timestamp(ledger_timestamp) or meta.get("ledger_timestamp") or meta["created_at"]
    meta.setdefault("ingested_at", ingested_at)

    used_daemon, result = maybe_daemon_request(
        "add",
        {
            "content": content,
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "metadata": meta or None,
            "infer": not no_infer,
        },
    )
    if not used_daemon:
        result = memory_client().add(
            content,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            metadata=meta or None,
            infer=not no_infer,
        )
    if isinstance(result, dict):
        result.setdefault("duration_ms", int((time.perf_counter() - start) * 1000))
    finished_at = now_utc_iso()
    append_live_audit(
        operation="add",
        input_payload={
            "text": text,
            "messages": messages,
            "file": str(file) if file else None,
            "content": content,
            "infer": not no_infer,
        },
        metadata=meta,
        result=result,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((time.perf_counter() - start) * 1000),
        scope=scope_dict(user_id, agent_id, app_id, run_id),
    )
    output(
        result,
        command="add",
        fmt=chosen_format(output_format, json_flag),
        scope=scope_dict(user_id, agent_id, app_id, run_id),
    )


@app.command()
def search(
    query: Optional[str] = typer.Argument(None, help="Search query."),
    user_id: str = typer.Option(DEFAULT_USER_ID, "--user-id", "-u", help="Workspace user scope."),
    top_k: int = typer.Option(10, "--top-k", "-k", "--limit", help="Number of results."),
    threshold: float = typer.Option(0.1, "--threshold", help="Minimum score threshold."),
    rerank: bool = typer.Option(False, "--rerank", help="Use configured OpenRouter LLM reranker."),
    keyword: bool = typer.Option(False, "--keyword", help="Accepted for official CLI compatibility."),
    fields: Optional[str] = typer.Option(None, "--fields", help="Accepted for official CLI compatibility."),
    explain: bool = typer.Option(False, "--explain", help="Return retrieval explanation when supported."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, table, quiet"),
) -> None:
    """Query local memory using semantic or hybrid retrieval."""
    del keyword, fields
    if query is None and stdin_is_piped():
        query = sys.stdin.read().strip()
    if not query:
        raise typer.BadParameter("Search query cannot be empty.")
    if top_k < 1:
        raise typer.BadParameter("--top-k must be >= 1.")

    filters = filters_from_scope(user_id, None, None, None)
    used_daemon, result = maybe_daemon_request(
        "search",
        {
            "query": query,
            "top_k": top_k,
            "filters": filters,
            "threshold": threshold,
            "rerank": rerank,
            "explain": explain,
        },
    )
    if not used_daemon:
        result = memory_client().search(
            query,
            top_k=top_k,
            filters=filters,
            threshold=threshold,
            rerank=rerank,
            explain=explain,
        )
    output(
        result,
        command="search",
        fmt=chosen_format(output_format, json_flag),
        scope=scope_dict(user_id, None, None, None),
    )


@app.command("list")
def list_memories(
    user_id: str = typer.Option(DEFAULT_USER_ID, "--user-id", "-u", help="Filter by user."),
    page: int = typer.Option(1, "--page", help="Page number."),
    page_size: int = typer.Option(100, "--page-size", help="Results per page."),
    filter_json: list[str] = typer.Option([], "--filter", help="JSON object or key=value filter."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("table", "--output", "-o", help="text, json, table, quiet"),
) -> None:
    """List local memories with optional filters."""
    if page < 1:
        raise typer.BadParameter("--page must be >= 1.")
    if page_size < 1:
        raise typer.BadParameter("--page-size must be >= 1.")

    extra = parse_json_or_key_values(filter_json, option_name="--filter")
    filters = filters_from_scope(user_id, None, None, None, extra)
    start = (page - 1) * page_size
    used_daemon, result = maybe_daemon_request(
        "list",
        {
            "filters": filters or None,
            "top_k": page * page_size,
            "start": start,
            "end": start + page_size,
        },
    )
    if not used_daemon:
        raw = memory_client().get_all(filters=filters or None, top_k=page * page_size)
        items = normalize_items(raw)
        result = items[start : start + page_size]
    output(
        result,
        command="list",
        fmt=chosen_format(output_format, json_flag),
        scope=scope_dict(user_id, None, None, None),
    )


@app.command()
def get(
    memory_id: str = typer.Argument(..., help="Memory ID to retrieve."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Get a memory by ID."""
    used_daemon, result = maybe_daemon_request("get", {"memory_id": memory_id})
    if not used_daemon:
        result = memory_client().get(memory_id)
    output(result, command="get", fmt=chosen_format(output_format, json_flag))


@app.command()
def update(
    memory_id: str = typer.Argument(..., help="Memory ID to update."),
    text: str = typer.Argument(..., help="Replacement memory text."),
    metadata: list[str] = typer.Option([], "--metadata", "-m", help="JSON object or key=value."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Update a memory by ID."""
    start = time.perf_counter()
    started_at = now_utc_iso()
    used_daemon, existing = maybe_daemon_request("get", {"memory_id": memory_id})
    client = None
    if not used_daemon:
        client = memory_client()
        existing = client.get(memory_id)
    existing_meta = existing.get("metadata") or {}
    meta = {**existing_meta, **parse_json_or_key_values(metadata, option_name="--metadata")}
    if existing.get("created_at"):
        meta["created_at"] = existing["created_at"]
    meta.setdefault("ledger_timestamp", existing_meta.get("ledger_timestamp") or existing.get("created_at") or now_utc_iso())
    update_context = detect_writer_context()
    updater_agent_id = update_context.get("source") or MANUAL_SOURCE
    updater_session_id = update_context.get("session_id") or MANUAL_SESSION
    meta.setdefault("memory_schema_version", MEMORY_SCHEMA_VERSION)
    meta["updated_by_cli_at"] = now_utc_iso()
    meta["last_updated_by_agent_id"] = updater_agent_id
    meta["last_updated_session_id"] = updater_session_id
    if used_daemon:
        used_update_daemon, result = maybe_daemon_request(
            "update",
            {"memory_id": memory_id, "text": text, "metadata": meta},
        )
        if not used_update_daemon:
            raise click.ClickException("mem0-local daemon became unavailable during update")
    else:
        result = client.update(memory_id, text, metadata=meta)
    finished_at = now_utc_iso()
    append_live_audit(
        operation="update",
        input_payload={
            "memory_id": memory_id,
            "text": text,
            "metadata_options": metadata,
            "existing": existing,
        },
        metadata=meta,
        result=result,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((time.perf_counter() - start) * 1000),
        scope=scope_dict(existing.get("user_id"), existing.get("agent_id"), None, existing.get("run_id")),
    )
    output(result, command="update", fmt=chosen_format(output_format, json_flag))


@app.command()
def delete(
    memory_id: Optional[str] = typer.Argument(None, help="Memory ID to delete."),
    all_: bool = typer.Option(False, "--all", help="Delete all memories matching scope."),
    user_id: str = typer.Option(DEFAULT_USER_ID, "--user-id", "-u", help="Scope to user."),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Scope to agent."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Scope to run."),
    force: bool = typer.Option(False, "--force", help="Required for --all."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Delete one memory, or delete all memories in a scope."""
    start = time.perf_counter()
    started_at = now_utc_iso()
    if all_:
        if not force:
            raise typer.BadParameter("--all requires --force.")
        used_daemon, result = maybe_daemon_request(
            "delete",
            {"all": True, "user_id": user_id, "agent_id": agent_id, "run_id": run_id},
        )
        if not used_daemon:
            result = memory_client().delete_all(user_id=user_id, agent_id=agent_id, run_id=run_id)
        finished_at = now_utc_iso()
        append_live_audit(
            operation="delete_all",
            input_payload={"all": True, "force": force},
            metadata=None,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((time.perf_counter() - start) * 1000),
            scope=scope_dict(user_id, agent_id, None, run_id),
        )
        output(
            result,
            command="delete",
            fmt=chosen_format(output_format, json_flag),
            scope=scope_dict(user_id, agent_id, None, run_id),
        )
        return
    if not memory_id:
        raise typer.BadParameter("Pass memory_id or --all --force.")
    used_get_daemon, existing = maybe_daemon_request("get", {"memory_id": memory_id})
    client = None
    if not used_get_daemon:
        client = memory_client()
        existing = client.get(memory_id)
    used_daemon, result = maybe_daemon_request("delete", {"all": False, "memory_id": memory_id})
    if not used_daemon:
        result = (client or memory_client()).delete(memory_id)
    wrapped_result = {"id": memory_id, "result": result}
    finished_at = now_utc_iso()
    append_live_audit(
        operation="delete",
        input_payload={"memory_id": memory_id, "existing": existing},
        metadata=(existing.get("metadata") if isinstance(existing, dict) else None),
        result=wrapped_result,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((time.perf_counter() - start) * 1000),
        scope=scope_dict(
            existing.get("user_id") if isinstance(existing, dict) else None,
            existing.get("agent_id") if isinstance(existing, dict) else None,
            None,
            existing.get("run_id") if isinstance(existing, dict) else None,
        ),
    )
    output(wrapped_result, command="delete", fmt=chosen_format(output_format, json_flag))


@app.command()
def history(
    memory_id: str = typer.Argument(..., help="Memory ID to inspect."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show Mem0 history for a memory when available."""
    used_daemon, result = maybe_daemon_request("history", {"memory_id": memory_id})
    if not used_daemon:
        result = memory_client().history(memory_id)
    output(result, command="history", fmt=chosen_format(output_format, json_flag))


@app.command("embed-test")
def embed_test(text: str = typer.Argument(..., help="Text to embed.")) -> None:
    """Verify the local fastembed model."""
    setup_env()
    from fastembed import TextEmbedding

    vector = list(TextEmbedding(model_name=EMBEDDING_MODEL).embed([text]))[0]
    output({"model": EMBEDDING_MODEL, "dims": len(vector), "first": float(vector[0])}, command="embed-test", fmt="json")


def cli_main() -> None:
    app(prog_name="mem0-local")


if __name__ == "__main__":
    cli_main()
