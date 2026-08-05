"""The root commands: the memory lifecycle a session actually types."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.app.command()
def status(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show local memory store configuration."""
    _support.setup_env()
    data = {
        "backend": "local",
        "root": str(_support.ROOT),
        "config_path": str(_support.CONFIG_PATH) if _support.CONFIG_PATH else None,
        "collection": _support.COLLECTION,
        "vector_store": _support.VECTOR_STORE_MODE,
        "qdrant_server": (
            f"{_support.VECTOR_STORE_HOST}:{_support.VECTOR_STORE_PORT}"
            if _support.VECTOR_STORE_MODE == "qdrant-server"
            else None
        ),
        "qdrant_path": str(_support.QDRANT_DIR),
        "history_db_path": str(_support.HISTORY_DB),
        "mem0_dir": os.environ["MEM0_DIR"],
        "fastembed_cache_path": os.environ["FASTEMBED_CACHE_PATH"],
        "embedder": {"provider": _support.EMBEDDING_PROVIDER, "model": _support.EMBEDDING_MODEL, "dims": _support.EMBEDDING_DIMS},
        # One row per job, because "which model does this run on" now has six
        # answers and no default. A job whose table is unresolvable reports the
        # error here rather than at the call that needed it.
        "llm": {job: _support._llm_job_status(job) for job in _support.LLM_JOBS},
        "auto_context": _support.detect_writer_context(),
    }
    _support.output(data, command="status", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
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
    result = _support.run_invalidate(memory_id, by_ids, reason=reason, raise_on_error=True)
    _support.output(result, command="invalidate", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
def revive(
    memory_id: str = typer.Argument(..., help="Invalidated memory ID to restore."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Clear supersession state so a memory re-enters the default search pool."""
    context = _support.detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "actor_id": context.get("source") or _support.MANUAL_SOURCE,
        "session_id": context.get("session_id") or _support.MANUAL_SESSION,
    }
    try:
        with _support.audited("revive", input_payload=op_args) as span:
            span.result = _support.execute("revive", op_args)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    _support.output(span.result, command="revive", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
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
    result = _support._apply_ttl(memory_id, days=days, clear=clear)
    _support.output(result, command="ttl", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
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
    from memline.review import session_review
    from memline.staleness import pair_store

    session = session or _support.detect_writer_context().get("session_id")
    if not session:
        raise typer.BadParameter("No session id detected; pass --session explicitly.")
    payload = session_review(
        session,
        execute=_support.execute,
        queue_factory=_support._event_queue_direct,
        pairs=pair_store(),
        wait=wait,
        user_id=_support.DEFAULT_USER_ID,
    )
    _support.output(payload, command="review", fmt=_support.chosen_format(output_format, json_flag))
    # Opt-in so existing callers that ignore the exit code keep working.
    if check and payload["blocking"]:
        raise typer.Exit(code=2)


@_support.app.command()
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
    result = _support.execute(
        "list",
        {
            "filters": {"user_id": _support.DEFAULT_USER_ID, "ingested_at": {"gte": since}},
            "top_k": limit,
            "start": 0,
            "end": limit,
        },
    )
    items = _support.normalize_items(result) or (result if isinstance(result, list) else [])
    items = sorted(
        items,
        key=lambda x: (x.get("metadata") or {}).get("ingested_at") or x.get("created_at") or "",
        reverse=True,
    )
    _support.output(
        {"since": since, "count": len(items), "memories": items},
        command="start",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.app.command()
def add(
    text: Optional[str] = typer.Argument(None, help="Text content to add."),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u", help="Scope to user."),
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
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow non-Latin text in a raw write. Only bypasses the language gate; never the raw-write length cap.",
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
    is queued in the background — check with `memline event status <id>`,
    or pass --wait for the synchronous path.
    """
    start = time.perf_counter()
    infer = infer_opt if infer_opt is not None else bool(messages or file)
    supersede_ids = [s.strip() for s in (supersedes or "").split(",") if s.strip()]
    if supersede_ids and infer:
        raise typer.BadParameter(
            "--supersedes requires the raw add path (extraction adds are async and have no id yet)."
        )
    content = _support.read_content(text, messages, file)
    detected_non_latin: list[str] = []
    if not infer:
        # The cap is absolute: --force only applies after this check passes.
        _support.check_raw_length(content)
        detected_non_latin = _support.check_raw_language(content, force=force)
    meta = _support.parse_json_or_key_values(metadata, option_name="--metadata")
    auto_context = _support.detect_writer_context()
    if auto_context.get("source"):
        meta.setdefault("source", auto_context["source"])
    if auto_context.get("session_id"):
        meta.setdefault("session_id", auto_context["session_id"])
    meta.setdefault("source", _support.MANUAL_SOURCE)
    if agent_id is None:
        agent_id = str(meta.get("source") or _support.MANUAL_SOURCE)
    if run_id is None:
        run_id = str(meta.get("session_id") or _support.MANUAL_SESSION)
    meta.setdefault("session_id", run_id)
    meta.setdefault("writer_agent_id", agent_id)
    meta.setdefault("origin", "ledger_import" if meta.get("source") == "agent-memory-ledger" else "live_agent")
    meta.setdefault("memory_schema_version", _support.MEMORY_SCHEMA_VERSION)
    if app_id:
        meta.setdefault("app_id", app_id)
    ingested_at = _support.now_utc_iso()
    created_at = _support.normalize_timestamp(timestamp) or meta.get("created_at") or ingested_at
    meta["created_at"] = _support.normalize_timestamp(str(created_at))
    meta["ledger_timestamp"] = _support.normalize_timestamp(ledger_timestamp) or meta.get("ledger_timestamp") or meta["created_at"]
    meta.setdefault("ingested_at", ingested_at)

    if infer and not wait:
        used_daemon, queued = _support.maybe_daemon_request(
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
            _support.output(
                queued,
                command="add",
                fmt=_support.chosen_format(output_format, json_flag),
                scope=_support.scope_dict(user_id, agent_id, app_id, run_id),
            )
            return

    result: Any = None
    add_error: Optional[Exception] = None
    with _support.audited(
        "add",
        input_payload={
            "text": text,
            "messages": messages,
            "file": str(file) if file else None,
            "content": content,
            "infer": infer,
            "language_override": bool(force and detected_non_latin),
            "non_latin_character_count": len(detected_non_latin),
        },
        metadata=meta,
        scope=_support.scope_dict(user_id, agent_id, app_id, run_id),
    ) as span:
        try:
            result = _support.execute(
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
            (item.get("id") for item in _support.normalize_items(result) if item.get("id")), None
        )
        if new_id is None:
            result_note = {"error": "no new memory id in add result; nothing invalidated"}
            if isinstance(result, dict):
                result["supersedes"] = result_note
        else:
            outcomes = []
            for old_id in supersede_ids:
                outcomes.append(
                    _support.run_invalidate(
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
            (item.get("id") for item in _support.normalize_items(result) if item.get("id")), None
        )
        if new_id:
            try:
                from memline.queue import EventQueue

                stale_event = EventQueue().enqueue(
                    "stale_check",
                    {"new_id": str(new_id), "session_id": meta.get("session_id")},
                )
                if isinstance(result, dict):
                    result["stale_check_event"] = stale_event
            except Exception:  # noqa: BLE001
                pass
    _support.output(
        result,
        command="add",
        fmt=_support.chosen_format(output_format, json_flag),
        scope=_support.scope_dict(user_id, agent_id, app_id, run_id),
    )


@_support.app.command()
def search(
    query: Optional[str] = typer.Argument(None, help="Search query."),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u", help="Workspace user scope."),
    top_k: int = typer.Option(10, "--top-k", "-k", "--limit", help="Number of results."),
    threshold: float = typer.Option(
        0.1,
        "--threshold",
        help="Minimum score threshold. Local default 0.1 (official CLI uses 0.3): intentional — hybrid vector+BM25 scores here are distributed lower than Platform scores.",
    ),
    rerank: bool = typer.Option(False, "--rerank", help="Score results with the configured rerank endpoint."),
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
    if query is None and _support.stdin_is_piped():
        query = sys.stdin.read().strip()
    if not query:
        raise typer.BadParameter("Search query cannot be empty.")
    if top_k < 1:
        raise typer.BadParameter("--top-k must be >= 1.")

    filters = _support.filters_from_scope(user_id, None, None, None)
    result = _support.execute(
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
    result = _support.project_fields(result, fields)
    _support.output(
        result,
        command="search",
        fmt=_support.chosen_format(output_format, json_flag),
        scope=_support.scope_dict(user_id, None, None, None),
    )


@_support.app.command("list")
def list_memories(
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u", help="Filter by user."),
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

    extra = _support.parse_json_or_key_values(filter_json, option_name="--filter")
    filters = _support.filters_from_scope(user_id, None, None, None, extra)
    start = (page - 1) * page_size
    result = _support.execute(
        "list",
        {
            "filters": filters or None,
            "top_k": page * page_size,
            "start": start,
            "end": start + page_size,
        },
    )
    _support.output(
        result,
        command="list",
        fmt=_support.chosen_format(output_format, json_flag),
        scope=_support.scope_dict(user_id, None, None, None),
    )


@_support.app.command()
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
    result = _support.execute("get", {"memory_id": memory_id})
    if resolve_head and isinstance(result, dict):
        try:
            result["head"] = _support.execute("resolve_head", {"memory_id": memory_id})
        except Exception as exc:  # noqa: BLE001 - head resolution must not mask the get.
            result["head"] = {"error": str(exc)}
    _support.output(result, command="get", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
def update(
    memory_id: str = typer.Argument(..., help="Memory ID to update."),
    text: str = typer.Argument(..., help="Replacement memory text."),
    metadata: list[str] = typer.Option([], "--metadata", "-m", help="JSON object or key=value."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Update a memory by ID."""
    existing = _support.execute("get", {"memory_id": memory_id})
    if not isinstance(existing, dict):
        raise click.ClickException(f"memory not found: {memory_id}")
    _support.check_raw_length(text, previous=str(existing.get("memory") or existing.get("data") or ""))
    meta = _support.updated_memory_metadata(
        existing, _support.parse_json_or_key_values(metadata, option_name="--metadata")
    )
    with _support.audited(
        "update",
        input_payload={
            "memory_id": memory_id,
            "text": text,
            "metadata_options": metadata,
            "existing": existing,
        },
        metadata=meta,
        scope=_support.scope_dict(existing.get("user_id"), existing.get("agent_id"), None, existing.get("run_id")),
    ) as span:
        span.result = _support.execute("update", {"memory_id": memory_id, "text": text, "metadata": meta})
    # Re-judge the rewritten text. Suspicion pairs are keyed on the old text
    # hash, so an update expires every flag standing against this memory;
    # without this the corrected text is never checked and a half-finished fix
    # (e.g. a correctness rewrite that left Chinese narration in place) escapes
    # review permanently. Self-checks only — the point is to re-examine this
    # entry, not to re-scan its neighbors. Enqueue failure must never break the
    # update, which has already been committed and audited above.
    try:
        from memline.queue import EventQueue

        stale_event = EventQueue().enqueue(
            "stale_check",
            {
                "new_id": str(memory_id),
                "session_id": _support.detect_writer_context().get("session_id"),
                "self_only": True,
            },
        )
        if isinstance(span.result, dict):
            span.result["stale_check_event"] = stale_event
    except Exception:  # noqa: BLE001
        pass
    _support.output(span.result, command="update", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
def delete(
    memory_id: Optional[str] = typer.Argument(None, help="Memory ID to delete."),
    all_: bool = typer.Option(False, "--all", help="Delete all memories matching scope."),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u", help="Scope to user."),
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
            filters = _support.filters_from_scope(user_id, agent_id, None, run_id)
            matches = _support.execute(
                "list",
                {"filters": filters or None, "top_k": 10000, "start": 0, "end": 10000},
            )
            matches = _support.normalize_items(matches) or (matches if isinstance(matches, list) else [])
            _support.output(
                {
                    "dry_run": True,
                    "would_delete_count": len(matches),
                    "sample": [
                        {"id": m.get("id"), "memory": m.get("memory") or m.get("data")}
                        for m in matches[:10]
                    ],
                },
                command="delete",
                fmt=_support.chosen_format(output_format, json_flag),
                scope=_support.scope_dict(user_id, agent_id, None, run_id),
            )
            return
        if not force:
            raise typer.BadParameter("--all requires --force.")
        with _support.audited(
            "delete_all",
            input_payload={"all": True, "force": force},
            scope=_support.scope_dict(user_id, agent_id, None, run_id),
        ) as span:
            span.result = _support.execute(
                "delete",
                {"all": True, "user_id": user_id, "agent_id": agent_id, "run_id": run_id},
            )
        _support.output(
            span.result,
            command="delete",
            fmt=_support.chosen_format(output_format, json_flag),
            scope=_support.scope_dict(user_id, agent_id, None, run_id),
        )
        return
    if not memory_id:
        raise typer.BadParameter("Pass memory_id or --all --force.")
    existing = _support.execute("get", {"memory_id": memory_id})
    if dry_run:
        _support.output(
            {"dry_run": True, "would_delete": existing},
            command="delete",
            fmt=_support.chosen_format(output_format, json_flag),
        )
        return
    preview = ""
    if isinstance(existing, dict):
        preview = str(existing.get("memory") or existing.get("data") or "")[:80]
    _support.confirm_destructive(f"Delete memory {memory_id} ({preview!r})?", force)
    with _support.audited(
        "delete",
        input_payload={"memory_id": memory_id, "existing": existing},
        metadata=(existing.get("metadata") if isinstance(existing, dict) else None),
        scope=_support.scope_dict(
            existing.get("user_id") if isinstance(existing, dict) else None,
            existing.get("agent_id") if isinstance(existing, dict) else None,
            None,
            existing.get("run_id") if isinstance(existing, dict) else None,
        ),
    ) as span:
        context = _support.detect_writer_context()
        result = _support.execute(
            "delete",
            {
                "all": False,
                "memory_id": memory_id,
                "actor_id": context.get("source") or _support.MANUAL_SOURCE,
            },
        )
        downgraded = isinstance(result, dict) and result.get("downgraded_to_expiry")
        if not downgraded:
            try:
                from memline.staleness import pair_store

                pair_store().close_for_deleted_memory(memory_id)
            except Exception:  # noqa: BLE001 - pair hygiene must never break delete.
                pass
        span.result = {"id": memory_id, "result": result}
    _support.output(span.result, command="delete", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command()
def history(
    memory_id: str = typer.Argument(..., help="Memory ID to inspect."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show Mem0 history for a memory when available."""
    result = _support.execute("history", {"memory_id": memory_id})
    _support.output(result, command="history", fmt=_support.chosen_format(output_format, json_flag))


@_support.app.command("embed-test")
def embed_test(text: str = typer.Argument(..., help="Text to embed.")) -> None:
    """Verify the local fastembed model."""
    _support.setup_env()
    from fastembed import TextEmbedding

    vector = list(TextEmbedding(model_name=_support.EMBEDDING_MODEL).embed([text]))[0]
    _support.output({"model": _support.EMBEDDING_MODEL, "dims": len(vector), "first": float(vector[0])}, command="embed-test", fmt="json")
