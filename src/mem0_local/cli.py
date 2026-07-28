#!/usr/bin/env python3
"""Local mem0-compatible CLI for the workspace memory store."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer
from rich.console import Console
from rich.table import Table

from mem0_local.audit import audited
from mem0_local.config import (
    COLLECTION,
    CONFIG_PATH,
    DEFAULT_USER_ID,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    HISTORY_DB,
    LLM_API_KEY_ENV,
    LLM_MODEL,
    LLM_BASE_URL,
    LLM_PROVIDER,
    LOCAL_TZ,
    MANUAL_SESSION,
    MANUAL_SOURCE,
    MAX_RAW_TEXT_CHARS,
    MEMORY_SCHEMA_VERSION,
    QDRANT_DIR,
    VECTOR_STORE_HOST,
    VECTOR_STORE_MODE,
    VECTOR_STORE_PORT,
    WORKSPACE_ROOT,
)
from mem0_local.ops import (
    dispatch as dispatch_op,
    dispatch_queue,
    op_timeout as daemon_operation_timeout,
)
from mem0_local.runtime import (
    get_client,
    normalize_items,
    require_llm_api_key,
    setup_env,
)

ROOT = WORKSPACE_ROOT

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
entity_app = typer.Typer(help="Inspect and manage the local entity graph.")
app.add_typer(entity_app, name="entity")
event_app = typer.Typer(help="Inspect background add-processing events (async queue).")
app.add_typer(event_app, name="event")
stale_app = typer.Typer(help="Inspect and dispose staleness suspicions (advisory judge output).")
app.add_typer(stale_app, name="stale")
protected_app = typer.Typer(help="Inspect temporary displacement protections.")
stale_app.add_typer(protected_app, name="protected")

agent_mode = False


def memory_client():
    """Build (or reuse) the direct-path client, with CLI-side guard checks."""
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
    try:
        require_llm_api_key()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from None
    return get_client()


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


def autostart_enabled() -> bool:
    return os.environ.get("MEM0_LOCAL_NO_AUTOSTART", "").lower() not in {"1", "true", "yes", "on"}


def daemon_spawn_safe() -> bool:
    """Heuristic gate: only auto-spawn the daemon from a host-visible context.

    From an isolated sandbox namespace, a spawned daemon lives in an overlay
    (invisible to other processes, dies with the sandbox) and may try to start
    a duplicate qdrant against the shared storage. Signals used:
    - qdrant reachable over TCP -> host network visible, spawning is safe;
    - qdrant process visible but TCP unreachable -> network-isolated, refuse;
    - neither -> genuine cold host if pid 1 is the real init, else refuse.
    """
    import subprocess

    try:
        from mem0_local.daemon import _qdrant_reachable
        from mem0_local.config import VECTOR_STORE_MODE

        if VECTOR_STORE_MODE == "qdrant-server":
            if _qdrant_reachable():
                return True
            if subprocess.run(["pgrep", "-x", "qdrant"], capture_output=True, timeout=5).returncode == 0:
                return False
        comm = Path("/proc/1/comm").read_text().strip().lower()
        return comm.startswith(("init", "systemd"))
    except Exception:  # noqa: BLE001 - when unsure, do not spawn.
        return False


_autostart_attempted = False


def maybe_daemon_request(op: str, args: dict[str, Any]) -> tuple[bool, Any]:
    global _autostart_attempted
    if not daemon_enabled():
        return False, None
    try:
        from mem0_local.daemon import DaemonUnavailable, PID_PATH, SOCKET_PATH, request, start_daemon
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
        if not autostart_enabled() or _autostart_attempted:
            return False, None
        if not daemon_spawn_safe():
            err_console.print(
                "[dim]mem0-local: daemon not running and this context cannot safely spawn it "
                "(isolated sandbox?); using direct path. After a reboot, run "
                "`mem0-local daemon start` from a normal shell.[/dim]"
            )
            return False, None
        # Lazy auto-start: bring the daemon (and, via its startup, the qdrant
        # server) up on first use instead of requiring a manual start after
        # every WSL reboot. Concurrent CLI callers serialize on the daemon
        # start lock; losers find the winner's socket via ping.
        _autostart_attempted = True
        err_console.print("[dim]mem0-local: daemon not running; auto-starting (may take ~15s after a reboot)...[/dim]")
        try:
            start_daemon(wait_seconds=90.0)
        except Exception as start_exc:  # noqa: BLE001 - degrade to direct path.
            err_console.print(
                f"[yellow]mem0-local: daemon auto-start failed ({start_exc}); "
                "falling back to the direct path. Set MEM0_LOCAL_NO_AUTOSTART=1 to silence.[/yellow]"
            )
            return False, None
        try:
            return True, request({"op": op, "args": args}, timeout=daemon_operation_timeout(op, args))
        except Exception as retry_exc:
            raise click.ClickException(
                f"mem0-local daemon {op} failed after auto-start: {retry_exc}"
            ) from retry_exc
    except Exception as exc:
        raise click.ClickException(f"mem0-local daemon {op} failed: {exc}") from exc


def execute(op: str, args: dict[str, Any]) -> Any:
    """Run one store op through the daemon when available, else in-process.

    Both paths run the same handler from ``mem0_local.ops``, so daemon and
    direct execution cannot drift apart semantically.
    """
    used_daemon, result = maybe_daemon_request(op, args)
    if used_daemon:
        return result
    return dispatch_op(memory_client(), op, args)


def execute_queue(op: str, args: dict[str, Any]) -> Any:
    """Queue-plane op: daemon when available, else the local event queue."""
    used_daemon, result = maybe_daemon_request(op, args)
    if used_daemon:
        return result
    return dispatch_queue(_event_queue_direct(), op, args)


def confirm_destructive(prompt: str, force: bool) -> None:
    """Official-CLI-style guard: confirm on a TTY, require --force otherwise."""
    if force:
        return
    if sys.stdin.isatty() and sys.stderr.isatty():
        if not typer.confirm(prompt):
            raise typer.Abort()
        return
    raise click.ClickException(
        "Refusing destructive operation in non-interactive mode without --force "
        "(use --dry-run to preview)."
    )


def project_fields(result: Any, fields: Optional[str]) -> Any:
    """Project each result item to the requested comma-separated fields (id is always kept)."""
    if not fields:
        return result
    wanted = [f.strip() for f in fields.split(",") if f.strip()]

    def proj(item: dict[str, Any]) -> dict[str, Any]:
        projected: dict[str, Any] = {"id": item.get("id")}
        for key in wanted:
            if key != "id":
                projected[key] = item.get(key)
        return projected

    if isinstance(result, dict) and isinstance(result.get("results"), list):
        projected_result = dict(result)
        projected_result["results"] = [proj(x) for x in result["results"] if isinstance(x, dict)]
        return projected_result
    if isinstance(result, list):
        return [proj(x) for x in result if isinstance(x, dict)]
    return result


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
    return detect_writer_context().get("source") in {"codex", "claude", "opencode"}


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


def read_proc_argv0(pid: int) -> str:
    """Return only argv[0] (the executable path) for a pid.

    Reads just the first NUL-delimited field of /proc/<pid>/cmdline, so
    process-based agent detection can never see user-supplied argv content
    such as the memory text being written.
    """
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    first = raw.split(b"\x00", 1)[0]
    return first.decode(errors="replace").strip()


def ancestor_exe_names(limit: int = 10) -> list[str]:
    """Basenames of ancestor process executables (argv[0] only).

    Excludes the current process and never inspects full command lines, so the
    memory content being written cannot be mistaken for an agent identity.
    """
    names: list[str] = []
    pid = os.getpid()
    for _ in range(limit):
        ppid = read_proc_ppid(pid)
        if not ppid or ppid <= 1 or ppid == pid:
            break
        pid = ppid
        argv0 = read_proc_argv0(pid)
        if argv0:
            names.append(os.path.basename(argv0).lower())
    return names


# Ordered agent identity table: (source, hard identity env vars, argv0 markers).
# Detection uses these signals only -- never the memory content being written.
# Order matters when agents are nested (e.g. `opencode` started from a claude
# shell inherits CLAUDECODE): the innermost agent must win, so sources whose
# identity vars are injected per shell invocation come first.
_AGENT_IDENTITY: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        # OPENCODE_SESSION_ID / OPENCODE_CALL_ID are injected per shell call by
        # the mem0-local opencode plugin (.opencode/plugin/mem0-local.js).
        "opencode",
        ("OPENCODE_SESSION_ID", "OPENCODE_CALL_ID"),
        ("opencode",),
    ),
    (
        "codex",
        ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_MANAGED_PACKAGE_ROOT"),
        ("codex",),
    ),
    (
        "claude",
        (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDECODE_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDECODE",
        ),
        ("claude",),
    ),
)


def detect_agent_source() -> str | None:
    """Identify the writing agent from hard signals only.

    Priority: per-agent identity env vars, then the generic AI_AGENT/AGENT tag,
    then ancestor executable names. Never inspects the memory content or the
    full command line, so what is being written cannot change the attribution.
    """
    # 1. Dedicated per-agent identity env vars (hardest signal).
    for source, env_names, _markers in _AGENT_IDENTITY:
        if first_env(*env_names):
            return source
    # 2. Generic agent tag, e.g. AI_AGENT="claude-code_2-1-205_agent" -> "claude".
    tag = first_env("AI_AGENT", "AGENT")
    if tag:
        token = re.split(r"[^A-Za-z0-9]+", tag.strip().lower(), maxsplit=1)[0]
        if token:
            return token
    # 3. Last resort: ancestor executable basenames (content-safe, argv[0] only).
    names = ancestor_exe_names()
    for source, _env_names, markers in _AGENT_IDENTITY:
        if any(marker in name for name in names for marker in markers):
            return source
    return None


def detect_writer_context() -> dict[str, str]:
    """Best-effort local caller detection for audit metadata.

    Both source and session_id come from hard signals only -- explicit MEM0_*
    overrides, per-agent identity env vars, the AI_AGENT tag, and ancestor
    executable names. The memory content being written is never inspected.
    """
    source = first_env("MEM0_LOCAL_SOURCE", "MEM0_SOURCE", "AGENT_SOURCE", "AI_AGENT_SOURCE")
    session_id = first_env(
        "MEM0_LOCAL_SESSION_ID",
        "MEM0_SESSION_ID",
        "AGENT_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE_SESSION_ID",
    )
    if not source:
        source = detect_agent_source()

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

    if command == "event-list":
        items = data if isinstance(data, list) else []
        if not items:
            console.print("No events found.")
            return
        table = Table("Event", "Status", "Attempts", "Updated", "Content", "Error")
        for item in items:
            table.add_row(
                str(item.get("event_id", ""))[:12],
                str(item.get("status") or ""),
                str(item.get("attempts") or 0),
                str(item.get("updated_at") or "")[:19],
                str(item.get("content_preview") or "")[:60],
                str(item.get("error") or "")[:40],
            )
        console.print(table)
        return

    if command == "entity-list":
        items = data if isinstance(data, list) else []
        if not items:
            console.print("No entities found.")
            return
        table = Table("ID", "Type", "Entity", "Links")
        for item in items:
            table.add_row(
                str(item.get("id", ""))[:12],
                str(item.get("entity_type") or ""),
                str(item.get("data") or ""),
                str(len(item.get("linked_memory_ids") or [])),
            )
        console.print(table)
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


def check_raw_length(content: Any, *, previous: Optional[str] = None) -> None:
    """Hard cap for verbatim (raw) writes: one atomic fact per entry.

    An update may keep or shrink an over-cap legacy entry (redaction and
    correction must never be blocked) but cannot grow it past the cap.
    """
    if not isinstance(content, str):
        return
    n = len(content)
    if n <= MAX_RAW_TEXT_CHARS:
        return
    if previous is not None and n <= len(previous):
        return
    raise click.ClickException(
        f"text is {n} chars, over the raw-write hard cap of {MAX_RAW_TEXT_CHARS}. "
        "Split it into multiple single-fact `add` calls (one atomic, self-contained "
        "fact per entry — retrieval works per fact), or pass --infer to let LLM "
        "extraction break long content up. "
        "Cap is configurable via [memory].max_raw_text_chars."
    )


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


# Hygiene-state banners (failed queue events, open staleness suspicions) only
# surface on the commands where that state is actionable; routine add/search
# stays quiet — the maintenance rules tell agents to ignore hygiene state
# mid-task anyway. The session-handoff banner is exempt: interrupting routine
# calls is its purpose.
HYGIENE_BANNER_COMMANDS = {"review", "stale", "event", "status"}


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
) -> None:
    global agent_mode
    agent_mode = json_output
    if ctx.invoked_subcommand in HYGIENE_BANNER_COMMANDS:
        try:
            from mem0_local.queue import read_alerts

            alerts = read_alerts()
            if alerts and alerts.get("failed_unacked"):
                err_console.print(
                    f"[yellow]mem0-local: {alerts['failed_unacked']} queued event(s) FAILED. "
                    "Inspect with `mem0-local event list --status failed`, then "
                    "`mem0-local event retry <event_id>` or `mem0-local event ack --all`.[/yellow]"
                )
        except Exception:  # noqa: BLE001 - the banner must never break a command.
            pass
        try:
            from mem0_local.staleness import pair_store

            open_pairs = pair_store().open_count()
            if open_pairs:
                err_console.print(
                    f"[yellow]mem0-local: {open_pairs} open staleness suspicion(s) await review. "
                    "List with `mem0-local stale list`; dispose with "
                    "`mem0-local stale confirm|dismiss <pair_id>` or `mem0-local review`.[/yellow]"
                )
        except Exception:  # noqa: BLE001 - the banner must never break a command.
            pass
    try:
        from mem0_local.config import SESSION_ADD_ALERT_THRESHOLD
        from mem0_local.session_stats import session_stats_store

        if SESSION_ADD_ALERT_THRESHOLD > 0:
            session_id = detect_writer_context().get("session_id")
            if session_id:
                session_adds = session_stats_store().add_count(session_id)
                if session_adds > SESSION_ADD_ALERT_THRESHOLD:
                    err_console.print(
                        f"[yellow]mem0-local: this session has accumulated {session_adds} memory "
                        f"add(s) (threshold {SESSION_ADD_ALERT_THRESHOLD}). Consider wrapping up: "
                        "tell the user this session is due for a handoff, and run the "
                        "end-of-session handoff review (`mem0-local review`) when they agree.[/yellow]"
                    )
    except Exception:  # noqa: BLE001 - the banner must never break a command.
        pass


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
        "vector_store": VECTOR_STORE_MODE,
        "qdrant_server": (
            f"{VECTOR_STORE_HOST}:{VECTOR_STORE_PORT}"
            if VECTOR_STORE_MODE == "qdrant-server"
            else None
        ),
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


def run_invalidate(
    memory_id: str,
    by_ids: list[str],
    *,
    reason: str | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Invalidate one memory, audited; per-id errors become result rows unless raising."""
    context = detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "by_ids": by_ids,
        "reason": reason,
        "actor_id": context.get("source") or MANUAL_SOURCE,
        "session_id": context.get("session_id") or MANUAL_SESSION,
    }
    error: Exception | None = None
    with audited("invalidate", input_payload=op_args) as span:
        try:
            span.result = execute("invalidate", op_args)
        except Exception as exc:  # noqa: BLE001 - audited, surfaced per flag.
            error = exc
            span.result = {"id": memory_id, "invalidated": False, "error": str(exc)}
    if error is not None and raise_on_error:
        if isinstance(error, click.ClickException):
            raise error
        raise click.ClickException(str(error)) from error
    return span.result


@app.command()
def invalidate(
    memory_id: str = typer.Argument(..., help="Memory ID to mark as superseded."),
    by: str = typer.Option(..., "--by", help="Comma-separated superseding memory id(s)."),
    reason: Optional[str] = typer.Option(None, "--reason", help="Short human/agent-readable reason."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Mark a memory as superseded: it leaves the default search pool.

    Text, history, and manifests are preserved; reverse with `revive`.
    """
    by_ids = [s.strip() for s in by.split(",") if s.strip()]
    if not by_ids:
        raise typer.BadParameter("--by requires at least one memory id.")
    result = run_invalidate(memory_id, by_ids, reason=reason, raise_on_error=True)
    output(result, command="invalidate", fmt=chosen_format(output_format, json_flag))


@app.command()
def revive(
    memory_id: str = typer.Argument(..., help="Invalidated memory ID to restore."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Clear supersession state so a memory re-enters the default search pool."""
    context = detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "actor_id": context.get("source") or MANUAL_SOURCE,
        "session_id": context.get("session_id") or MANUAL_SESSION,
    }
    try:
        with audited("revive", input_payload=op_args) as span:
            span.result = execute("revive", op_args)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    output(span.result, command="revive", fmt=chosen_format(output_format, json_flag))


def _interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _fetch_memory(memory_id: str) -> dict[str, Any] | None:
    try:
        row = execute("get", {"memory_id": memory_id})
        return row if isinstance(row, dict) else None
    except Exception:  # noqa: BLE001 - preview only.
        return None


def _enrich_pairs(pairs: list[dict[str, Any]], *, preview_chars: int = 160) -> list[dict[str, Any]]:
    enriched = []
    for pair in pairs:
        item = dict(pair)
        for side in ("old", "new"):
            row = _fetch_memory(pair[f"{side}_id"])
            item[f"{side}_memory"] = (
                str(row.get("memory") or "")[:preview_chars] if row else "<deleted>"
            )
        enriched.append(item)
    return enriched


def updated_memory_metadata(
    existing: dict[str, Any], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Metadata for rewriting an existing memory: preserve creation
    timestamps, record the updater (used by both `update` and `stale merge`)."""
    existing_meta = existing.get("metadata") or {}
    meta = dict(existing_meta)
    if extra:
        meta.update(extra)
    if existing.get("created_at"):
        meta["created_at"] = existing["created_at"]
    meta.setdefault(
        "ledger_timestamp",
        existing_meta.get("ledger_timestamp") or existing.get("created_at") or now_utc_iso(),
    )
    context = detect_writer_context()
    meta.setdefault("memory_schema_version", MEMORY_SCHEMA_VERSION)
    meta["updated_by_cli_at"] = now_utc_iso()
    meta["last_updated_by_agent_id"] = context.get("source") or MANUAL_SOURCE
    meta["last_updated_session_id"] = context.get("session_id") or MANUAL_SESSION
    return meta


def _apply_ttl(
    memory_id: str,
    *,
    days: float | None = None,
    clear: bool = False,
    expire_now: bool = False,
) -> dict[str, Any]:
    """Set/clear a reversible pool-exit deadline on one memory, audited."""
    context = detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "days": days,
        "clear": clear,
        "expire_now": expire_now,
        "actor_id": context.get("source") or MANUAL_SOURCE,
    }
    with audited("ttl", input_payload=op_args) as span:
        span.result = execute("set_ttl", op_args)
    return span.result


@app.command()
def ttl(
    memory_id: str = typer.Argument(..., help="Memory ID to schedule (or clear) expiry for."),
    days: Optional[float] = typer.Option(None, "--days", help="Days until the entry leaves the search pool (default 7)."),
    clear: bool = typer.Option(False, "--clear", help="Remove any scheduled/materialized expiry (re-enters the pool)."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Schedule reversible expiry: the entry leaves default search at the
    deadline (lazily enforced; the daemon materializes it later). Reverse
    any time with --clear."""
    result = _apply_ttl(memory_id, days=days, clear=clear)
    output(result, command="ttl", fmt=chosen_format(output_format, json_flag))


# Legacy 5-way necessity verdicts map onto the two disposition families
# (pre-v5 marks recorded by the 2026-07 backlog scan).
_EXPIRING_VERDICTS = {"EXPIRING", "PROGRESS_TICK", "EVENT_SCOPED"}


def _flag_suggestion(pair: dict[str, Any]) -> str:
    """Reviewer guidance per suspicion kind/verdict (lifecycle design R4)."""
    if pair.get("kind") == "correctness":
        return (
            "fix via `update <memory_id> '<corrected text>'` (expires this flag) | "
            "false positive -> `stale dismiss <pair_id>`"
        )
    if pair.get("kind") == "safety":
        return (
            "PRIORITY — suspected plaintext credential: redact via "
            "`update <memory_id> '<same text with the secret replaced by its "
            "location pointer>'` (keeps id/links/history, expires this flag), "
            "then verify `search '<secret>' --include-superseded` returns "
            "nothing | false positive -> `stale dismiss <pair_id>`"
        )
    if pair.get("kind") == "ttl_expiry":
        return (
            "entry already left the pool at its deadline: accept -> "
            "`stale confirm <pair_id>` | still needed -> `stale ttl <pair_id> "
            "[--days 7]` (renews and re-enters the pool)"
        )
    if pair.get("verdict") in _EXPIRING_VERDICTS:
        return (
            "settled/closed -> `stale confirm <pair_id>` (expires now) | "
            "still alive -> `stale ttl <pair_id> [--days 7]` | "
            "false positive -> `stale dismiss <pair_id>`"
        )
    return (
        "verify born-unnecessary -> `stale confirm <pair_id>` (expires now), or "
        "`delete <memory_id>` for your own fresh entry | keep the non-derivable "
        "part -> `update <memory_id> '<rewritten>'` | false positive -> "
        "`stale dismiss <pair_id>`"
    )


def _load_open_pair(pair_id: str) -> tuple[Any, dict[str, Any]]:
    """Fetch a suspicion pair and require it to be open."""
    from mem0_local.staleness import pair_store

    store = pair_store()
    pair = store.get(pair_id)
    if not pair:
        raise click.ClickException(f"pair not found: {pair_id}")
    if pair["disposition"] != "open":
        raise click.ClickException(f"pair is not open (disposition={pair['disposition']})")
    return store, pair


def _require_disposition_authority(pair: dict[str, Any], force: bool, action: str) -> None:
    """Design rule: non-interactive sessions dispose only their own writes.

    ttl-expiry flags are sessionless lifecycle events — any session running
    review may dispose them (accepting an already-effective expiry or
    renewing is safe in both directions).
    """
    if force:
        return
    if (pair.get("kind") or "displacement") == "ttl_expiry":
        return
    own_session = detect_writer_context().get("session_id")
    if own_session and pair.get("new_session_id") == own_session:
        return
    if _interactive_tty():
        pair_id = pair.get("pair_id") or pair.get("id") or "<unknown>"
        if click.confirm(
            f"Pair {pair_id} belongs to another session. Confirm {action}?",
            default=False,
        ):
            return
        raise click.ClickException(f"{action} cancelled: cross-session confirmation declined")
    raise click.ClickException(
        f"{action} denied: non-interactive sessions may only dispose "
        "suspicions raised by their own writes (design: disposition "
        "authority). Ask the user to confirm from an interactive session, "
        "or pass --force when the user has approved it out-of-band."
    )


def _dispose_with_rollback(
    store: Any, pair_id: str, disposition: str, actor: str, mutate: Any
) -> Any:
    """Dispose first (so a follow-up invalidate's close_for_old cannot
    relabel this pair), run the follow-up mutation, and reopen the pair on
    failure so it stays reviewable instead of stranding half-executed."""
    store.dispose(pair_id, disposition, disposed_by=actor)
    try:
        return mutate()
    except Exception:
        store.reopen(pair_id)
        raise


@stale_app.command("list")
def stale_list(
    session: Optional[str] = typer.Option(None, "--session", help="Only pairs raised by this session's writes."),
    limit: int = typer.Option(100, "--limit", help="Max pairs returned."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """List open staleness suspicions (advisory; dispose with confirm/dismiss)."""
    from mem0_local.staleness import pair_store

    pairs = pair_store().open_pairs(session_id=session, limit=limit)
    output(_enrich_pairs(pairs), command="stale-list", fmt=chosen_format(output_format, json_flag))


@stale_app.command("confirm")
def stale_confirm(
    pair_id: str = typer.Argument(..., help="Open suspicion pair id (see `stale list`)."),
    force: bool = typer.Option(False, "--force", help="The user approved this disposition out-of-band; skips the interactive-session gate."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Confirm a suspicion: invalidate the old memory (superseded by the new one).

    Non-interactive callers may only confirm pairs raised by their own
    session's writes; cross-session backlog needs an interactive session or
    explicit user approval recorded via --force.
    """
    store, pair = _load_open_pair(pair_id)
    kind = pair.get("kind") or "displacement"
    if kind == "correctness":
        raise click.ClickException(
            "correctness suspicions are corrected via `update` (which expires the "
            "flag) or closed with `stale dismiss`; confirm does not apply."
        )
    _require_disposition_authority(pair, force, "confirm")
    actor = detect_writer_context().get("source") or MANUAL_SOURCE

    def mutate() -> dict[str, Any]:
        if kind == "ttl_expiry":
            # The entry already left the pool at its deadline; confirming
            # simply accepts that state.
            return {"expiry_accepted": pair["old_id"]}
        if kind == "necessity":
            # A self-suspicion has no superseder; confirming it means the
            # entry leaves the pool now via reversible expiry — materialized
            # directly, so the harvest loop never re-asks about a decision
            # the reviewer just made.
            return {"expired": _apply_ttl(pair["old_id"], expire_now=True)}
        return {
            "invalidate": run_invalidate(
                pair["old_id"],
                [pair["new_id"]],
                reason=pair.get("reason") or f"confirmed staleness suspicion {pair_id}",
                raise_on_error=True,
            )
        }

    result = _dispose_with_rollback(store, pair_id, "confirmed", actor, mutate)
    output(
        {"pair_id": pair_id, "disposition": "confirmed", **result},
        command="stale-confirm",
        fmt=chosen_format(output_format, json_flag),
    )


@stale_app.command("ttl")
def stale_ttl(
    pair_id: str = typer.Argument(..., help="Open necessity suspicion pair id."),
    days: Optional[float] = typer.Option(None, "--days", help="Days until natural expiry (default 7)."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Dispose a snapshot-type suspicion as still-alive: keep the entry in
    the pool now, let it expire naturally at the deadline (reversible via
    `ttl <memory_id> --clear`)."""
    store, pair = _load_open_pair(pair_id)
    if (pair.get("kind") or "displacement") not in {"necessity", "ttl_expiry"}:
        raise click.ClickException(
            "ttl disposition applies to necessity and ttl-expiry suspicions; "
            "displacement pairs use confirm/dismiss/merge, timestamp flags "
            "use update/dismiss."
        )
    actor = detect_writer_context().get("source") or MANUAL_SOURCE
    result = _dispose_with_rollback(
        store, pair_id, "ttl", actor, lambda: _apply_ttl(pair["old_id"], days=days)
    )
    output(
        {"pair_id": pair_id, "disposition": "ttl", "ttl": result},
        command="stale-ttl",
        fmt=chosen_format(output_format, json_flag),
    )


@stale_app.command("dismiss")
def stale_dismiss(
    pair_id: str = typer.Argument(..., help="Open suspicion pair id."),
    force: bool = typer.Option(False, "--force", help="The user approved this cross-session disposition out-of-band."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Dismiss a suspicion (pair-level, permanent)."""
    store, pair = _load_open_pair(pair_id)
    _require_disposition_authority(pair, force, "dismiss")
    actor = detect_writer_context().get("source") or MANUAL_SOURCE
    store.dispose(pair_id, "dismissed", disposed_by=actor)
    result: dict[str, Any] = {"pair_id": pair_id, "disposition": "dismissed"}
    output(result, command="stale-dismiss", fmt=chosen_format(output_format, json_flag))


@stale_app.command("protect")
def stale_protect(
    memory_id: str = typer.Argument(..., help="Memory id to protect from displacement judging."),
    kind: str = typer.Option("displacement", "--kind", help="Only displacement is supported."),
    days: float = typer.Option(30.0, "--days", help="Protection duration in days (default 30, maximum 90)."),
    reason: str = typer.Option(..., "--reason", help="Required human-readable reason."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Temporarily suppress displacement judging for one memory.

    Necessity, correctness, and safety checks are never suppressed. The latest
    three independent displacement suspicions for the current text
    version must all have been dismissed. The core setter enforces this rule.
    """
    from mem0_local.staleness import (
        MAX_DISPLACEMENT_PROTECTION_DAYS,
        MAX_DISPLACEMENT_PROTECTION_REASON_CHARS,
    )

    if kind != "displacement":
        raise typer.BadParameter("--kind only supports 'displacement'.")
    reason = reason.strip()
    if not reason:
        raise typer.BadParameter("--reason must not be empty.")
    if len(reason) > MAX_DISPLACEMENT_PROTECTION_REASON_CHARS:
        raise typer.BadParameter(
            "--reason must be <= "
            f"{MAX_DISPLACEMENT_PROTECTION_REASON_CHARS} characters."
        )
    if days <= 0 or days > MAX_DISPLACEMENT_PROTECTION_DAYS:
        raise typer.BadParameter(
            f"--days must be > 0 and <= {MAX_DISPLACEMENT_PROTECTION_DAYS}."
        )
    context = detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "days": days,
        "reason": reason,
        "actor_id": context.get("source") or MANUAL_SOURCE,
        "session_id": context.get("session_id") or MANUAL_SESSION,
    }
    with audited("displacement_protect", input_payload=op_args) as span:
        span.result = execute("set_displacement_protection", op_args)
    output(
        span.result,
        command="stale-protect",
        fmt=chosen_format(output_format, json_flag),
    )


@stale_app.command("unprotect")
def stale_unprotect(
    memory_id: str = typer.Argument(..., help="Memory id whose displacement protection should be removed."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Remove displacement protection; already-dismissed pairs stay closed."""
    context = detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "actor_id": context.get("source") or MANUAL_SOURCE,
        "cause": "manual",
    }
    with audited("displacement_unprotect", input_payload=op_args) as span:
        span.result = execute("clear_displacement_protection", op_args)
    output(
        span.result,
        command="stale-unprotect",
        fmt=chosen_format(output_format, json_flag),
    )


@protected_app.command("list")
def stale_protected_list(
    include_expired: bool = typer.Option(False, "--include-expired", help="Also show elapsed protection records."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """List displacement-protected memories."""
    rows = execute(
        "list_displacement_protections",
        {
            "user_id": DEFAULT_USER_ID,
            "scan_limit": 10000,
            "include_expired": include_expired,
        },
    )
    output(
        rows,
        command="stale-protected-list",
        fmt=chosen_format(output_format, json_flag),
    )


@stale_app.command("merge")
def stale_merge(
    pair_id: str = typer.Argument(..., help="Open suspicion pair id."),
    merged_text: str = typer.Argument(..., help="Consolidated text carrying both entries' still-valid facts."),
    force: bool = typer.Option(False, "--force", help="The user approved this disposition out-of-band; skips the interactive-session gate."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Merge a pair: the newer memory is updated to the consolidated text and
    the older memory is invalidated (superseded by it).

    For suspicions where the new entry adds detail rather than replacing the
    old answer: one consolidated entry stays in the pool, the old original is
    preserved in history/manifests as usual. Same authority rule as confirm.
    """
    store, pair = _load_open_pair(pair_id)
    if (pair.get("kind") or "displacement") != "displacement":
        raise click.ClickException("merge applies only to displacement pairs.")
    _require_disposition_authority(pair, force, "merge")
    new_id, old_id = pair["new_id"], pair["old_id"]
    # Mirrors the update command's metadata handling; audited as an update.
    existing = execute("get", {"memory_id": new_id})
    if not isinstance(existing, dict):
        raise click.ClickException(f"memory not found: {new_id}")
    meta = updated_memory_metadata(existing, {"merged_from": old_id})
    with audited(
        "update",
        input_payload={"memory_id": new_id, "text": merged_text, "merge_pair_id": pair_id, "existing": existing},
        metadata=meta,
        scope=scope_dict(existing.get("user_id"), existing.get("agent_id"), None, existing.get("run_id")),
    ) as span:
        span.result = execute(
            "update", {"memory_id": new_id, "text": merged_text, "metadata": meta}
        )
    actor = detect_writer_context().get("source") or MANUAL_SOURCE
    # Same rollback rule as confirm: reopen the pair if the invalidate fails
    # (the consolidated update already landed and is retryable).
    invalidate_result = _dispose_with_rollback(
        store, pair_id, "merged", actor,
        lambda: run_invalidate(
            old_id, [new_id], reason=f"merged into {new_id} (pair {pair_id})", raise_on_error=True
        ),
    )
    output(
        {
            "pair_id": pair_id,
            "disposition": "merged",
            "updated": new_id,
            "invalidate": invalidate_result,
        },
        command="stale-merge",
        fmt=chosen_format(output_format, json_flag),
    )


@app.command()
def review(
    session: Optional[str] = typer.Option(None, "--session", help="Session id; defaults to the detected current session."),
    wait: bool = typer.Option(False, "--wait", help="Wait (up to 120s) for pending background judgments first."),
    check: bool = typer.Option(False, "--check", help="Exit 2 instead of 0 when the verdict is 'blocked'."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Handoff review: this session's writes plus the suspicions they raised.

    Reports an acceptance `verdict`. It is "pass" only when everything THIS
    session wrote has been handled -- its writes' own safety/correctness/
    necessity flags, the retirements its writes raised, and any queued write
    of its own that failed to land -- and no TTL expiry is left unreviewed.
    Displacement pairs raised by other sessions are deliberately out of scope:
    a session disposes only its own writes (design: disposition authority), so
    that backlog never blocks a pass. TTL expiries are the exception to that
    exception -- disposition authority for them is granted to every session,
    so they are nobody's property and would otherwise be nobody's obligation.

    Dispose each listed pair with `stale confirm <pair_id>` /
    `stale dismiss <pair_id>`, or correct the old memory with
    `update`. Undisposed pairs persist and surface via the CLI banner.
    """
    from mem0_local.staleness import pair_store

    session = session or detect_writer_context().get("session_id")
    if not session:
        raise typer.BadParameter("No session id detected; pass --session explicitly.")

    def pending_stale_checks() -> int:
        queue = _event_queue_direct()
        count = 0
        for row in queue.list(limit=500):
            if row["op"] == "stale_check" and row["status"] in {"queued", "processing"}:
                args = queue.get_args(row["event_id"]) or {}
                if args.get("session_id") == session:
                    count += 1
        return count

    pending = pending_stale_checks()
    if wait:
        deadline = time.time() + 120
        while pending and time.time() < deadline:
            time.sleep(3)
            pending = pending_stale_checks()

    filters = {"user_id": DEFAULT_USER_ID, "run_id": session}
    writes = execute("list", {"filters": filters, "top_k": 500, "start": 0, "end": 500})
    write_items = normalize_items(writes) or (writes if isinstance(writes, list) else [])

    all_pairs = pair_store().open_pairs(session_id=session)
    displacement = [p for p in all_pairs if (p.get("kind") or "displacement") == "displacement"]

    def flag_view(p: dict[str, Any]) -> dict[str, Any]:
        row = _fetch_memory(p["old_id"])
        return {
            "pair_id": p["pair_id"],
            "kind": p["kind"],
            "memory_id": p["old_id"],
            "verdict": p["verdict"],
            "confidence": p["confidence"],
            "reason": p["reason"],
            "memory": str(row.get("memory") or "")[:160] if row else "<deleted>",
            "suggested": _flag_suggestion(p),
        }

    # Disposition order (design 2026-07-21): safety first (redact before
    # anything else can invalidate and strand the plaintext), then correctness
    # (fix the fact), then necessity (may then invalidate). Displacement is
    # handled last, from `open_pairs`.
    _SELF_FLAG_ORDER = {"safety": 0, "correctness": 1, "necessity": 2}
    self_flags = sorted(
        (
            flag_view(p)
            for p in all_pairs
            if (p.get("kind") or "displacement") not in {"displacement", "ttl_expiry"}
        ),
        key=lambda f: _SELF_FLAG_ORDER.get(f["kind"], 9),
    )
    # TTL expiries are lifecycle events, not this session's writes: any
    # session running review may dispose them (accept or renew).
    ttl_expired = [flag_view(p) for p in pair_store().open_pairs(kind="ttl_expiry")]

    # --- acceptance verdict -------------------------------------------------
    # Hard-coded so a session never has to infer "am I done?" from four lists.
    # Pairs raised by another session stay out: disposing those is not this
    # session's job and waiting on them would make the verdict unreachable in a
    # shared store. TTL expiries are in, because neither reason applies to them
    # — every session may dispose them, so the backlog is drainable by whoever
    # reaches handoff, and leaving it out lets a session pass while expired
    # entries pile up unreviewed with nobody obliged to look.
    def failed_own_adds() -> list[str]:
        queue = _event_queue_direct()
        out: list[str] = []
        for row in queue.list(status="failed", limit=500):
            args = queue.get_args(row["event_id"]) or {}
            if args.get("session_id") == session:
                out.append(row["event_id"])
        return out

    blocking: list[dict[str, Any]] = []
    if pending:
        blocking.append({
            "kind": "pending_stale_checks",
            "count": pending,
            "why": "this session's writes are still being judged; a verdict now would be premature",
            "how": "mem0-local review --wait",
        })
    for flag_kind in ("safety", "correctness", "necessity"):
        flagged = [f["memory_id"] for f in self_flags if f["kind"] == flag_kind]
        if flagged:
            blocking.append({
                "kind": f"self_flag:{flag_kind}",
                "count": len(flagged),
                "memory_ids": flagged,
                "why": f"this session's own writes carry unresolved {flag_kind} flags",
                "how": "see how_to_dispose",
            })
    if displacement:
        blocking.append({
            "kind": "displacement_raised_by_me",
            "count": len(displacement),
            "pair_ids": [p["pair_id"] for p in displacement],
            "why": "retirements that this session's writes raised are still undisposed",
            "how": "stale confirm|dismiss|merge <pair_id>, or update <old_id>",
        })
    if ttl_expired:
        blocking.append({
            "kind": "ttl_expired",
            "count": len(ttl_expired),
            "pair_ids": [p["pair_id"] for p in ttl_expired],
            "why": "entries whose TTL fired are unreviewed; any session may dispose them, "
                   "so this one is obliged to",
            "how": "stale confirm <pair_id> (accept the expiry) | stale ttl <pair_id> --days N (renew)",
        })
    failed_adds = failed_own_adds()
    if failed_adds:
        blocking.append({
            "kind": "failed_adds",
            "count": len(failed_adds),
            "event_ids": failed_adds,
            "why": "this session queued writes that never landed in the store",
            "how": "event retry <event_id> | event ack <event_id>",
        })
    verdict = "pass" if not blocking else "blocked"

    output(
        {
            "session": session,
            "verdict": verdict,
            "blocking": blocking,
            "writes_count": len(write_items),
            "writes": [
                {"id": w.get("id"), "memory": str(w.get("memory") or "")[:160]}
                for w in write_items
            ],
            "pending_stale_checks": pending,
            "open_pairs": _enrich_pairs(displacement),
            "self_flags": self_flags,
            "ttl_expired": ttl_expired,
            "how_to_dispose": (
                "self_flags are ordered by disposition priority: safety -> "
                "correctness -> necessity. "
                "safety: update <memory_id> '<secret replaced by its location pointer>' "
                "(redact in place, then verify search '<secret>' --include-superseded is empty) | stale dismiss <pair_id>. "
                "correctness: update <memory_id> '<corrected>' | stale dismiss <pair_id>. "
                "necessity: stale confirm (expire now) | stale ttl <pair_id> [--days 7] | "
                "delete <memory_id> (own fresh born-unnecessary entry) | stale dismiss. "
                "displacement (handle last): stale confirm <pair_id> | stale dismiss <pair_id> | "
                "stale merge <pair_id> '<consolidated text>' | update <old_id> '<corrected text>'. "
                "ttl_expiry: stale confirm (accept) | stale ttl <pair_id> (renew, re-enters pool)."
            ),
        },
        command="review",
        fmt=chosen_format(output_format, json_flag),
    )
    # Opt-in so existing callers that ignore the exit code keep working.
    if check and blocking:
        raise typer.Exit(code=2)


@app.command()
def start(
    days: float = typer.Option(1.0, "--days", help="Recall window in days (by ingested_at)."),
    limit: int = typer.Option(100, "--limit", help="Max entries returned."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Session bootstrap: recall recently ingested memories (default: last
    1 day), newest first. Use at the start of a new session, then `search`
    for task-specific recall."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = execute(
        "list",
        {
            "filters": {"user_id": DEFAULT_USER_ID, "ingested_at": {"gte": since}},
            "top_k": limit,
            "start": 0,
            "end": limit,
        },
    )
    items = normalize_items(result) or (result if isinstance(result, list) else [])
    items = sorted(
        items,
        key=lambda x: (x.get("metadata") or {}).get("ingested_at") or x.get("created_at") or "",
        reverse=True,
    )
    output(
        {"since": since, "count": len(items), "memories": items},
        command="start",
        fmt=chosen_format(output_format, json_flag),
    )


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
    infer_opt: Optional[bool] = typer.Option(
        None,
        "--infer/--no-infer",
        help="Force LLM extraction on/off. Default: raw storage for plain text; extraction for --messages/--file.",
    ),
    supersedes: Optional[str] = typer.Option(
        None,
        "--supersedes",
        help="Comma-separated memory ids this new entry supersedes (raw adds only): they are invalidated with superseded_by=<new id>.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="For extraction adds: wait synchronously instead of queueing (raw adds are always synchronous).",
    ),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Add a memory from text, messages, file, or stdin.

    Plain-text adds store the exact text verbatim (fast, synchronous, returns
    the memory id; exact re-fires dedup, semantic near-dups are annotated).
    Conversation input (--messages/--file) defaults to LLM extraction, which
    is queued in the background — check with `mem0-local event status <id>`,
    or pass --wait for the synchronous path.
    """
    start = time.perf_counter()
    infer = infer_opt if infer_opt is not None else bool(messages or file)
    supersede_ids = [s.strip() for s in (supersedes or "").split(",") if s.strip()]
    if supersede_ids and infer:
        raise typer.BadParameter(
            "--supersedes requires the raw add path (extraction adds are async and have no id yet)."
        )
    content = read_content(text, messages, file)
    if not infer:
        check_raw_length(content)
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

    if infer and not wait:
        used_daemon, queued = maybe_daemon_request(
            "add",
            {
                "content": content,
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "metadata": meta or None,
                "infer": True,
                "async": True,
            },
        )
        if used_daemon:
            # The daemon worker writes the manifest row at completion; the
            # enqueue ack itself is not a store mutation.
            if isinstance(queued, dict):
                queued.setdefault("duration_ms", int((time.perf_counter() - start) * 1000))
            output(
                queued,
                command="add",
                fmt=chosen_format(output_format, json_flag),
                scope=scope_dict(user_id, agent_id, app_id, run_id),
            )
            return

    result: Any = None
    add_error: Optional[Exception] = None
    with audited(
        "add",
        input_payload={
            "text": text,
            "messages": messages,
            "file": str(file) if file else None,
            "content": content,
            "infer": infer,
        },
        metadata=meta,
        scope=scope_dict(user_id, agent_id, app_id, run_id),
    ) as span:
        try:
            result = execute(
                "add",
                {
                    "content": content,
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "metadata": meta or None,
                    "infer": infer,
                },
            )
        except Exception as exc:  # mem0 raises on extraction failure; audit it before surfacing.
            add_error = exc
            span.result = {"error": str(exc)}
        else:
            if isinstance(result, dict):
                result.setdefault("duration_ms", int((time.perf_counter() - start) * 1000))
            span.result = result
    if add_error is not None:
        if isinstance(add_error, click.ClickException):
            raise add_error
        raise click.ClickException(f"add failed in mem0 backend: {add_error}") from add_error
    if supersede_ids:
        new_id = next(
            (item.get("id") for item in normalize_items(result) if item.get("id")), None
        )
        if new_id is None:
            result_note = {"error": "no new memory id in add result; nothing invalidated"}
            if isinstance(result, dict):
                result["supersedes"] = result_note
        else:
            outcomes = []
            for old_id in supersede_ids:
                outcomes.append(
                    run_invalidate(
                        old_id,
                        [str(new_id)],
                        reason=f"superseded at write time by {new_id}",
                    )
                )
            if isinstance(result, dict):
                result["supersedes"] = outcomes
    if not infer:
        # Advisory background staleness check for the new entry. Queued only —
        # the daemon judges it later; enqueue failure must never break the add.
        new_id = next(
            (item.get("id") for item in normalize_items(result) if item.get("id")), None
        )
        if new_id:
            try:
                from mem0_local.queue import EventQueue

                stale_event = EventQueue().enqueue(
                    "stale_check",
                    {"new_id": str(new_id), "session_id": meta.get("session_id")},
                )
                if isinstance(result, dict):
                    result["stale_check_event"] = stale_event
            except Exception:  # noqa: BLE001
                pass
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
    threshold: float = typer.Option(
        0.1,
        "--threshold",
        help="Minimum score threshold. Local default 0.1 (official CLI uses 0.3): intentional — hybrid vector+BM25 scores here are distributed lower than Platform scores.",
    ),
    rerank: bool = typer.Option(False, "--rerank", help="Use configured OpenRouter LLM reranker."),
    keyword: bool = typer.Option(False, "--keyword", help="Pure BM25 keyword retrieval instead of hybrid semantic search."),
    include_superseded: bool = typer.Option(
        False,
        "--include-superseded",
        help="Also return invalidated (superseded) memories; default search filters them out.",
    ),
    fields: Optional[str] = typer.Option(None, "--fields", help="Comma-separated result fields to return (id always kept)."),
    explain: bool = typer.Option(False, "--explain", help="Return retrieval explanation when supported."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, table, quiet"),
) -> None:
    """Query local memory using hybrid semantic (default) or pure keyword retrieval."""
    if query is None and stdin_is_piped():
        query = sys.stdin.read().strip()
    if not query:
        raise typer.BadParameter("Search query cannot be empty.")
    if top_k < 1:
        raise typer.BadParameter("--top-k must be >= 1.")

    filters = filters_from_scope(user_id, None, None, None)
    result = execute(
        "search",
        {
            "query": query,
            "top_k": top_k,
            "filters": filters,
            "threshold": threshold,
            "rerank": rerank,
            "keyword": keyword,
            "explain": explain,
            "include_superseded": include_superseded,
        },
    )
    result = project_fields(result, fields)
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
    result = execute(
        "list",
        {
            "filters": filters or None,
            "top_k": page * page_size,
            "start": start,
            "end": start + page_size,
        },
    )
    output(
        result,
        command="list",
        fmt=chosen_format(output_format, json_flag),
        scope=scope_dict(user_id, None, None, None),
    )


@app.command()
def get(
    memory_id: str = typer.Argument(..., help="Memory ID to retrieve."),
    resolve_head: bool = typer.Option(
        False,
        "--resolve-head",
        help="Follow superseded_by pointers and also return the current active head(s).",
    ),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Get a memory by ID."""
    result = execute("get", {"memory_id": memory_id})
    if resolve_head and isinstance(result, dict):
        try:
            result["head"] = execute("resolve_head", {"memory_id": memory_id})
        except Exception as exc:  # noqa: BLE001 - head resolution must not mask the get.
            result["head"] = {"error": str(exc)}
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
    existing = execute("get", {"memory_id": memory_id})
    if not isinstance(existing, dict):
        raise click.ClickException(f"memory not found: {memory_id}")
    check_raw_length(text, previous=str(existing.get("memory") or existing.get("data") or ""))
    meta = updated_memory_metadata(
        existing, parse_json_or_key_values(metadata, option_name="--metadata")
    )
    with audited(
        "update",
        input_payload={
            "memory_id": memory_id,
            "text": text,
            "metadata_options": metadata,
            "existing": existing,
        },
        metadata=meta,
        scope=scope_dict(existing.get("user_id"), existing.get("agent_id"), None, existing.get("run_id")),
    ) as span:
        span.result = execute("update", {"memory_id": memory_id, "text": text, "metadata": meta})
    output(span.result, command="update", fmt=chosen_format(output_format, json_flag))


@app.command()
def delete(
    memory_id: Optional[str] = typer.Argument(None, help="Memory ID to delete."),
    all_: bool = typer.Option(False, "--all", help="Delete all memories matching scope."),
    user_id: str = typer.Option(DEFAULT_USER_ID, "--user-id", "-u", help="Scope to user."),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Scope to agent."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Scope to run."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation (required non-interactively; always required for --all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted without deleting."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Delete one memory, or delete all memories in a scope."""
    if all_:
        if dry_run:
            filters = filters_from_scope(user_id, agent_id, None, run_id)
            matches = execute(
                "list",
                {"filters": filters or None, "top_k": 10000, "start": 0, "end": 10000},
            )
            matches = normalize_items(matches) or (matches if isinstance(matches, list) else [])
            output(
                {
                    "dry_run": True,
                    "would_delete_count": len(matches),
                    "sample": [
                        {"id": m.get("id"), "memory": m.get("memory") or m.get("data")}
                        for m in matches[:10]
                    ],
                },
                command="delete",
                fmt=chosen_format(output_format, json_flag),
                scope=scope_dict(user_id, agent_id, None, run_id),
            )
            return
        if not force:
            raise typer.BadParameter("--all requires --force.")
        with audited(
            "delete_all",
            input_payload={"all": True, "force": force},
            scope=scope_dict(user_id, agent_id, None, run_id),
        ) as span:
            span.result = execute(
                "delete",
                {"all": True, "user_id": user_id, "agent_id": agent_id, "run_id": run_id},
            )
        output(
            span.result,
            command="delete",
            fmt=chosen_format(output_format, json_flag),
            scope=scope_dict(user_id, agent_id, None, run_id),
        )
        return
    if not memory_id:
        raise typer.BadParameter("Pass memory_id or --all --force.")
    existing = execute("get", {"memory_id": memory_id})
    if dry_run:
        output(
            {"dry_run": True, "would_delete": existing},
            command="delete",
            fmt=chosen_format(output_format, json_flag),
        )
        return
    preview = ""
    if isinstance(existing, dict):
        preview = str(existing.get("memory") or existing.get("data") or "")[:80]
    confirm_destructive(f"Delete memory {memory_id} ({preview!r})?", force)
    with audited(
        "delete",
        input_payload={"memory_id": memory_id, "existing": existing},
        metadata=(existing.get("metadata") if isinstance(existing, dict) else None),
        scope=scope_dict(
            existing.get("user_id") if isinstance(existing, dict) else None,
            existing.get("agent_id") if isinstance(existing, dict) else None,
            None,
            existing.get("run_id") if isinstance(existing, dict) else None,
        ),
    ) as span:
        context = detect_writer_context()
        result = execute(
            "delete",
            {
                "all": False,
                "memory_id": memory_id,
                "actor_id": context.get("source") or MANUAL_SOURCE,
            },
        )
        downgraded = isinstance(result, dict) and result.get("downgraded_to_expiry")
        if not downgraded:
            try:
                from mem0_local.staleness import pair_store

                pair_store().close_for_deleted_memory(memory_id)
            except Exception:  # noqa: BLE001 - pair hygiene must never break delete.
                pass
        span.result = {"id": memory_id, "result": result}
    output(span.result, command="delete", fmt=chosen_format(output_format, json_flag))


@app.command()
def history(
    memory_id: str = typer.Argument(..., help="Memory ID to inspect."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show Mem0 history for a memory when available."""
    result = execute("history", {"memory_id": memory_id})
    output(result, command="history", fmt=chosen_format(output_format, json_flag))


@entity_app.command("list")
def entity_list(
    entity_type: Optional[str] = typer.Option(None, "--type", help="Filter by entity type (PROPER, TOPIC, QUOTED, IDENTIFIER)."),
    contains: Optional[str] = typer.Option(None, "--contains", help="Case-insensitive substring filter on entity text."),
    page: int = typer.Option(1, "--page", help="Page number."),
    page_size: int = typer.Option(50, "--page-size", help="Results per page."),
    scan_limit: int = typer.Option(50000, "--scan-limit", help="Max entity rows scanned before filtering."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("table", "--output", "-o", help="text, json, table, quiet"),
) -> None:
    """List entity-graph rows, most-linked first."""
    if page < 1:
        raise typer.BadParameter("--page must be >= 1.")
    if page_size < 1:
        raise typer.BadParameter("--page-size must be >= 1.")
    start = (page - 1) * page_size
    result = execute(
        "entity_list",
        {
            "entity_type": entity_type,
            "contains": contains,
            "scan_limit": scan_limit,
            "start": start,
            "end": start + page_size,
        },
    )
    output(result, command="entity-list", fmt=chosen_format(output_format, json_flag))


@entity_app.command("delete")
def entity_delete(
    entity_id: str = typer.Argument(..., help="Entity ID to delete from the entity graph."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted without deleting."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation (required non-interactively)."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Delete one entity-graph row (memories themselves are not touched)."""
    row = execute("entity_get", {"entity_id": entity_id})
    if not row:
        raise click.ClickException(f"Entity not found: {entity_id}")
    if dry_run:
        output(
            {"dry_run": True, "would_delete": row},
            command="entity-delete",
            fmt=chosen_format(output_format, json_flag),
        )
        return
    linked = len(row.get("linked_memory_ids") or [])
    confirm_destructive(
        f"Delete entity {entity_id} ({row.get('data')!r}, {linked} linked memories)?", force
    )
    with audited(
        "entity_delete", input_payload={"entity_id": entity_id, "existing": row}
    ) as span:
        span.result = execute("entity_delete", {"entity_id": entity_id})
    output(span.result, command="entity-delete", fmt=chosen_format(output_format, json_flag))


def _event_queue_direct():
    from mem0_local.queue import EventQueue

    return EventQueue()


@event_app.command("list")
def event_list(
    status: Optional[str] = typer.Option(None, "--status", help="Filter: queued, processing, done, failed."),
    page: int = typer.Option(1, "--page", help="Page number."),
    page_size: int = typer.Option(50, "--page-size", help="Results per page."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("table", "--output", "-o", help="text, json, table, quiet"),
) -> None:
    """List background add events, newest first."""
    if status and status not in {"queued", "processing", "done", "failed"}:
        raise typer.BadParameter("--status must be one of: queued, processing, done, failed.")
    offset = (page - 1) * page_size
    result = execute_queue("event_list", {"status": status, "limit": page_size, "offset": offset})
    output(result, command="event-list", fmt=chosen_format(output_format, json_flag))


@event_app.command("status")
def event_status(
    event_id: str = typer.Argument(..., help="Event ID returned by an async add."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show one background event, including its result or error."""
    result = execute_queue("event_get", {"event_id": event_id})
    if not result:
        raise click.ClickException(f"Event not found: {event_id}")
    output(result, command="event-status", fmt=chosen_format(output_format, json_flag))


@event_app.command("retry")
def event_retry(
    event_id: str = typer.Argument(..., help="Failed event ID to requeue."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Requeue a failed event with its original input."""
    result = execute_queue("event_retry", {"event_id": event_id})
    output(result, command="event-retry", fmt=chosen_format(output_format, json_flag))


@event_app.command("ack")
def event_ack(
    event_id: Optional[str] = typer.Argument(None, help="Failed event ID to acknowledge."),
    all_: bool = typer.Option(False, "--all", help="Acknowledge all failed events."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Acknowledge failed events so the CLI warning banner clears."""
    if not event_id and not all_:
        raise typer.BadParameter("Pass an event ID or --all.")
    result = execute_queue("event_ack", {"event_id": None if all_ else event_id})
    output(result, command="event-ack", fmt=chosen_format(output_format, json_flag))


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
