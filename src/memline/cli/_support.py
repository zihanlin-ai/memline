#!/usr/bin/env python3
"""Local mem0-compatible CLI for the workspace memory store."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer
from rich.console import Console
from rich.table import Table

from memline.audit import audited
from memline.review import enrich_pairs
from memline.writer_context import detect_writer_context
from memline.config import (
    COLLECTION,
    CONFIG_PATH,
    DEFAULT_USER_ID,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    HISTORY_DB,
    LLM_JOBS,
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
from memline.ops import (
    dispatch as dispatch_op,
    dispatch_queue,
    op_timeout as daemon_operation_timeout,
)
from memline.runtime import (
    get_client,
    normalize_items,
    require_llm_api_key,
    setup_env,
)

ROOT = WORKSPACE_ROOT

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="memline",
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
# The wiki is a consumer of the store, not part of it: one namespace keeps that
# visible, and keeps the seam where this subsystem would be lifted out.
wiki_app = typer.Typer(help="Compile the workspace wiki from memories and designated documents.")
app.add_typer(wiki_app, name="wiki")

agent_mode = False


def memory_client():
    """Build (or reuse) the direct-path client, with CLI-side guard checks."""
    setup_env()
    if os.environ.get("MEMLINE_NO_DAEMON", "").lower() in {"1", "true", "yes", "on"}:
        try:
            from memline.daemon import status as daemon_status

            if daemon_status().get("running"):
                raise click.ClickException("memline daemon is running; stop it before using the direct path")
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
    value = os.environ.get("MEMLINE_NO_DAEMON", "")
    return value.lower() not in {"1", "true", "yes", "on"}


def autostart_enabled() -> bool:
    return os.environ.get("MEMLINE_NO_AUTOSTART", "").lower() not in {"1", "true", "yes", "on"}


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
        from memline.daemon import _qdrant_reachable
        from memline.config import VECTOR_STORE_MODE

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
        from memline.daemon import DaemonUnavailable, PID_PATH, SOCKET_PATH, request, start_daemon
    except Exception:
        return False, None
    try:
        return True, request({"op": op, "args": args}, timeout=daemon_operation_timeout(op, args))
    except DaemonUnavailable as exc:
        if SOCKET_PATH.exists() or PID_PATH.exists():
            raise click.ClickException(
                "memline daemon appears to be configured but is not reachable "
                f"({exc}). Run `memline daemon status`; if it is stale, run "
                "`memline daemon stop` and retry, or set MEMLINE_NO_DAEMON=1 "
                "for the direct path."
            ) from exc
        if not autostart_enabled() or _autostart_attempted:
            return False, None
        if not daemon_spawn_safe():
            err_console.print(
                "[dim]memline: daemon not running and this context cannot safely spawn it "
                "(isolated sandbox?); using direct path. After a reboot, run "
                "`memline daemon start` from a normal shell.[/dim]"
            )
            return False, None
        # Lazy auto-start: bring the daemon (and, via its startup, the qdrant
        # server) up on first use instead of requiring a manual start after
        # every WSL reboot. Concurrent CLI callers serialize on the daemon
        # start lock; losers find the winner's socket via ping.
        _autostart_attempted = True
        err_console.print("[dim]memline: daemon not running; auto-starting (may take ~15s after a reboot)...[/dim]")
        try:
            start_daemon(wait_seconds=90.0)
        except Exception as start_exc:  # noqa: BLE001 - degrade to direct path.
            err_console.print(
                f"[yellow]memline: daemon auto-start failed ({start_exc}); "
                "falling back to the direct path. Set MEMLINE_NO_AUTOSTART=1 to silence.[/yellow]"
            )
            return False, None
        try:
            return True, request({"op": op, "args": args}, timeout=daemon_operation_timeout(op, args))
        except Exception as retry_exc:
            raise click.ClickException(
                f"memline daemon {op} failed after auto-start: {retry_exc}"
            ) from retry_exc
    except Exception as exc:
        raise click.ClickException(f"memline daemon {op} failed: {exc}") from exc


def read_all_memories(user_id: str = DEFAULT_USER_ID) -> tuple[list[dict[str, Any]], str]:
    """Every memory, and the moment reading began.

    The moment is returned with the rows because a cursor must record when a
    run started reading, not when it finished: memories written while a long
    pass was in flight belong to the next run. Three commands used to page the
    store themselves, which meant three chances to disagree about that.
    """
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    memories: list[dict[str, Any]] = []
    page = 1
    while True:
        filters = filters_from_scope(user_id, None, None, None, {})
        got = execute("list", {"filters": filters or None, "top_k": page * 500,
                               "start": (page - 1) * 500, "end": page * 500})
        items = got if isinstance(got, list) else (got.get("results") or got.get("memories") or [])
        memories.extend(items)
        if len(items) < 500:
            return memories, started_at
        page += 1


def execute(op: str, args: dict[str, Any]) -> Any:
    """Run one store op through the daemon when available, else in-process.

    Both paths run the same handler from ``memline.ops``, so daemon and
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
    explicit = os.environ.get("MEMLINE_AUTO_JSON")
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


def non_latin_letters(content: Any) -> list[str]:
    """Return letter-like characters outside the Latin script.

    The raw-write gate is deterministic: it classifies Unicode letters and
    ideographs by their assigned character name, without a language model or
    ratio heuristic. Numbers, punctuation, symbols, emoji and combining marks
    do not trip the gate; Latin letters with diacritics remain allowed.
    """
    if not isinstance(content, str):
        return []
    return [
        char
        for char in content
        if unicodedata.category(char).startswith("L")
        and "LATIN" not in unicodedata.name(char, "")
    ]


def check_raw_language(content: Any, *, force: bool = False) -> list[str]:
    """Reject raw non-Latin narration unless explicitly overridden."""
    detected = non_latin_letters(content)
    if detected and not force:
        raise click.ClickException(
            "Non-English input detected. Rewrite the memory in English before "
            "adding it. If non-English content must be preserved, rerun the "
            "command with --force."
        )
    return detected


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
            from memline.queue import read_alerts

            alerts = read_alerts()
            if alerts and alerts.get("failed_unacked"):
                err_console.print(
                    f"[yellow]memline: {alerts['failed_unacked']} queued event(s) FAILED. "
                    "Inspect with `memline event list --status failed`, then "
                    "`memline event retry <event_id>` or `memline event ack --all`.[/yellow]"
                )
        except Exception:  # noqa: BLE001 - the banner must never break a command.
            pass
        try:
            from memline.staleness import pair_store

            open_pairs = pair_store().open_count()
            if open_pairs:
                err_console.print(
                    f"[yellow]memline: {open_pairs} open staleness suspicion(s) await review. "
                    "List with `memline stale list`; dispose with "
                    "`memline stale confirm|dismiss <pair_id>` or `memline review`.[/yellow]"
                )
        except Exception:  # noqa: BLE001 - the banner must never break a command.
            pass
    try:
        from memline.config import SESSION_ADD_ALERT_THRESHOLD
        from memline.session_stats import session_stats_store

        if SESSION_ADD_ALERT_THRESHOLD > 0:
            session_id = detect_writer_context().get("session_id")
            if session_id:
                session_adds = session_stats_store().add_count(session_id)
                if session_adds > SESSION_ADD_ALERT_THRESHOLD:
                    err_console.print(
                        f"[yellow]memline: this session has accumulated {session_adds} memory "
                        f"add(s) (threshold {SESSION_ADD_ALERT_THRESHOLD}). Consider wrapping up: "
                        "tell the user this session is due for a handoff, and run the "
                        "end-of-session handoff review (`memline review`) when they agree.[/yellow]"
                    )
    except Exception:  # noqa: BLE001 - the banner must never break a command.
        pass








def _llm_job_status(job: str) -> dict[str, Any]:
    """What this job resolves to, or why it does not resolve at all."""
    from memline.config import ConfigError, llm_endpoint_specs, llm_knobs

    try:
        specs = llm_endpoint_specs(job)
    except ConfigError as exc:
        return {"error": str(exc)}
    primary = specs[0]
    key_env = primary.get("api_key_env")
    return {
        "model": primary["model"],
        "base_url": primary["base_url"],
        "credential": key_env or primary.get("api_key_json"),
        # Whether the key is *present*, never the key.
        "credential_set": bool(os.environ.get(key_env)) if key_env else None,
        "fallbacks": [spec["model"] for spec in specs[1:]],
        "knobs": llm_knobs(job) or None,
    }




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






def _interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


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




# Per-verdict reviewer playbook. Keyed by the judge's own verdict, not just the
# suspicion kind, so review states what THIS finding means and which disposition
# actually resolves it. `dismiss_only_if` exists because dismissal is permanent
# for the judged text version: a flag closed on the wrong grounds can never be
# raised again. Added 2026-07-28 after an audit found a batch of LANGUAGE_SUSPECT
# flags dismissed without the entries being rewritten.
def _load_open_pair(pair_id: str) -> tuple[Any, dict[str, Any]]:
    """Fetch a suspicion pair and require it to be open."""
    from memline.staleness import pair_store

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








































def _count_types(suggestions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in suggestions:
        counts[item.get("type") or "?"] = counts.get(item.get("type") or "?", 0) + 1
    return dict(sorted(counts.items()))






def _review_artifacts(
    draft: Path, topics: Path | None = None
) -> tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Load a draft's sidecars and its approved topic without touching the store."""
    from memline.wiki.review import load_approved_topic

    stem = draft.with_suffix("")
    bundle_path = Path(str(stem) + ".bundle.json")
    claims_path = Path(str(stem) + ".claims.json")
    if not draft.is_file():
        raise typer.BadParameter(f"no draft: {draft}")
    if not bundle_path.is_file():
        raise typer.BadParameter(f"no bundle beside the draft: {bundle_path}")
    draft_text = draft.read_text(encoding="utf-8")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    claims = json.loads(claims_path.read_text(encoding="utf-8")) if claims_path.is_file() else None
    topic_key = (claims or {}).get("topic_key") or stem.name
    if topics is None:
        candidate = draft.parent.parent / "suggestions" / "accepted-topics.jsonl"
        topics = candidate if candidate.is_file() else None
    return draft_text, bundle, claims, load_approved_topic(topics, topic_key)


def _sanitize_review_artifacts(
    draft: Path,
    draft_text: str,
    bundle: dict[str, Any],
    claims: dict[str, Any] | None,
    topic: dict[str, Any] | None,
    sensitivity_review: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Apply current human rulings to old sidecars before an outbound review."""
    import copy

    from memline.bundle import Sanitizer
    from memline.wiki.draft import BLOCKING_FLAG_KINDS, load_review

    if sensitivity_review is None:
        candidate = draft.parent.parent / "sanitization-review.json"
        sensitivity_review = candidate if candidate.is_file() else None
    redactions, cleared = load_review(sensitivity_review)
    reviewed = set(redactions) | cleared
    blocking = sorted({
        flag.get("value") for flag in (bundle.get("sanitization") or {}).get("review_flags") or []
        if flag.get("kind") in BLOCKING_FLAG_KINDS and flag.get("value") not in reviewed
    })
    if blocking:
        raise typer.BadParameter(
            f"{len(blocking)} sensitive-looking value(s) remain unreviewed; "
            "refusing external review")
    leaked = [value for value in redactions if value in draft_text]
    if leaked:
        raise typer.BadParameter(
            f"the article still contains {len(leaked)} value(s) ruled for redaction; fix it first")

    sanitizer = Sanitizer(redactions)
    clean_bundle = copy.deepcopy(bundle)
    for memory in clean_bundle.get("memories") or []:
        memory["text"] = sanitizer.scrub(memory.get("text") or "")
    for source in clean_bundle.get("source_sections") or []:
        source["text"] = sanitizer.scrub(source.get("text") or "")

    def scrub_tree(value: Any) -> Any:
        if isinstance(value, str):
            return sanitizer.scrub(value)
        if isinstance(value, list):
            return [scrub_tree(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub_tree(item) for key, item in value.items()}
        return value

    return clean_bundle, scrub_tree(claims), scrub_tree(topic)
























def _event_queue_direct():
    from memline.queue import EventQueue

    return EventQueue()












def cli_main() -> None:
    app(prog_name="memline")
