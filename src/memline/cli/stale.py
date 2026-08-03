"""Staleness suspicions: list, dispose, protect. Authority checks live in _support."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.stale_app.command("list")
def stale_list(
    session: Optional[str] = typer.Option(None, "--session", help="Only pairs raised by this session's writes."),
    limit: int = typer.Option(100, "--limit", help="Max pairs returned."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """List open staleness suspicions (advisory; dispose with confirm/dismiss)."""
    from memline.staleness import pair_store

    pairs = pair_store().open_pairs(session_id=session, limit=limit)
    _support.output(_support.enrich_pairs(_support.execute, pairs), command="stale-list", fmt=_support.chosen_format(output_format, json_flag))


@_support.stale_app.command("confirm")
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
    store, pair = _support._load_open_pair(pair_id)
    kind = pair.get("kind") or "displacement"
    if kind == "correctness":
        raise click.ClickException(
            "correctness suspicions are corrected via `update` (which expires the "
            "flag) or closed with `stale dismiss`; confirm does not apply."
        )
    _support._require_disposition_authority(pair, force, "confirm")
    actor = _support.detect_writer_context().get("source") or _support.MANUAL_SOURCE

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
            return {"expired": _support._apply_ttl(pair["old_id"], expire_now=True)}
        return {
            "invalidate": _support.run_invalidate(
                pair["old_id"],
                [pair["new_id"]],
                reason=pair.get("reason") or f"confirmed staleness suspicion {pair_id}",
                raise_on_error=True,
            )
        }

    result = _support._dispose_with_rollback(store, pair_id, "confirmed", actor, mutate)
    _support.output(
        {"pair_id": pair_id, "disposition": "confirmed", **result},
        command="stale-confirm",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.stale_app.command("ttl")
def stale_ttl(
    pair_id: str = typer.Argument(..., help="Open necessity suspicion pair id."),
    days: Optional[float] = typer.Option(None, "--days", help="Days until natural expiry (default 7)."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Dispose a snapshot-type suspicion as still-alive: keep the entry in
    the pool now, let it expire naturally at the deadline (reversible via
    `ttl <memory_id> --clear`)."""
    store, pair = _support._load_open_pair(pair_id)
    if (pair.get("kind") or "displacement") not in {"necessity", "ttl_expiry"}:
        raise click.ClickException(
            "ttl disposition applies to necessity and ttl-expiry suspicions; "
            "displacement pairs use confirm/dismiss/merge, timestamp flags "
            "use update/dismiss."
        )
    actor = _support.detect_writer_context().get("source") or _support.MANUAL_SOURCE
    result = _support._dispose_with_rollback(
        store, pair_id, "ttl", actor, lambda: _support._apply_ttl(pair["old_id"], days=days)
    )
    _support.output(
        {"pair_id": pair_id, "disposition": "ttl", "ttl": result},
        command="stale-ttl",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.stale_app.command("dismiss")
def stale_dismiss(
    pair_id: str = typer.Argument(..., help="Open suspicion pair id."),
    force: bool = typer.Option(False, "--force", help="The user approved this cross-session disposition out-of-band."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Dismiss a suspicion (pair-level, permanent)."""
    store, pair = _support._load_open_pair(pair_id)
    _support._require_disposition_authority(pair, force, "dismiss")
    actor = _support.detect_writer_context().get("source") or _support.MANUAL_SOURCE
    store.dispose(pair_id, "dismissed", disposed_by=actor)
    result: dict[str, Any] = {"pair_id": pair_id, "disposition": "dismissed"}
    _support.output(result, command="stale-dismiss", fmt=_support.chosen_format(output_format, json_flag))


@_support.stale_app.command("protect")
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
    from memline.staleness import (
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
    context = _support.detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "days": days,
        "reason": reason,
        "actor_id": context.get("source") or _support.MANUAL_SOURCE,
        "session_id": context.get("session_id") or _support.MANUAL_SESSION,
    }
    with _support.audited("displacement_protect", input_payload=op_args) as span:
        span.result = _support.execute("set_displacement_protection", op_args)
    _support.output(
        span.result,
        command="stale-protect",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.stale_app.command("unprotect")
def stale_unprotect(
    memory_id: str = typer.Argument(..., help="Memory id whose displacement protection should be removed."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Remove displacement protection; already-dismissed pairs stay closed."""
    context = _support.detect_writer_context()
    op_args = {
        "memory_id": memory_id,
        "actor_id": context.get("source") or _support.MANUAL_SOURCE,
        "cause": "manual",
    }
    with _support.audited("displacement_unprotect", input_payload=op_args) as span:
        span.result = _support.execute("clear_displacement_protection", op_args)
    _support.output(
        span.result,
        command="stale-unprotect",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.protected_app.command("list")
def stale_protected_list(
    include_expired: bool = typer.Option(False, "--include-expired", help="Also show elapsed protection records."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """List displacement-protected memories."""
    rows = _support.execute(
        "list_displacement_protections",
        {
            "user_id": _support.DEFAULT_USER_ID,
            "scan_limit": 10000,
            "include_expired": include_expired,
        },
    )
    _support.output(
        rows,
        command="stale-protected-list",
        fmt=_support.chosen_format(output_format, json_flag),
    )


@_support.stale_app.command("merge")
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
    store, pair = _support._load_open_pair(pair_id)
    if (pair.get("kind") or "displacement") != "displacement":
        raise click.ClickException("merge applies only to displacement pairs.")
    _support._require_disposition_authority(pair, force, "merge")
    new_id, old_id = pair["new_id"], pair["old_id"]
    # Mirrors the update command's metadata handling; audited as an update.
    existing = _support.execute("get", {"memory_id": new_id})
    if not isinstance(existing, dict):
        raise click.ClickException(f"memory not found: {new_id}")
    meta = _support.updated_memory_metadata(existing, {"merged_from": old_id})
    with _support.audited(
        "update",
        input_payload={"memory_id": new_id, "text": merged_text, "merge_pair_id": pair_id, "existing": existing},
        metadata=meta,
        scope=_support.scope_dict(existing.get("user_id"), existing.get("agent_id"), None, existing.get("run_id")),
    ) as span:
        span.result = _support.execute(
            "update", {"memory_id": new_id, "text": merged_text, "metadata": meta}
        )
    actor = _support.detect_writer_context().get("source") or _support.MANUAL_SOURCE
    # Same rollback rule as confirm: reopen the pair if the invalidate fails
    # (the consolidated update already landed and is retryable).
    invalidate_result = _support._dispose_with_rollback(
        store, pair_id, "merged", actor,
        lambda: _support.run_invalidate(
            old_id, [new_id], reason=f"merged into {new_id} (pair {pair_id})", raise_on_error=True
        ),
    )
    _support.output(
        {
            "pair_id": pair_id,
            "disposition": "merged",
            "updated": new_id,
            "invalidate": invalidate_result,
        },
        command="stale-merge",
        fmt=_support.chosen_format(output_format, json_flag),
    )
