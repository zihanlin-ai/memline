"""The local entity graph."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.entity_app.command("list")
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
    result = _support.execute(
        "entity_list",
        {
            "entity_type": entity_type,
            "contains": contains,
            "scan_limit": scan_limit,
            "start": start,
            "end": start + page_size,
        },
    )
    _support.output(result, command="entity-list", fmt=_support.chosen_format(output_format, json_flag))


@_support.entity_app.command("delete")
def entity_delete(
    entity_id: str = typer.Argument(..., help="Entity ID to delete from the entity graph."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted without deleting."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation (required non-interactively)."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Delete one entity-graph row (memories themselves are not touched)."""
    row = _support.execute("entity_get", {"entity_id": entity_id})
    if not row:
        raise click.ClickException(f"Entity not found: {entity_id}")
    if dry_run:
        _support.output(
            {"dry_run": True, "would_delete": row},
            command="entity-delete",
            fmt=_support.chosen_format(output_format, json_flag),
        )
        return
    linked = len(row.get("linked_memory_ids") or [])
    _support.confirm_destructive(
        f"Delete entity {entity_id} ({row.get('data')!r}, {linked} linked memories)?", force
    )
    with _support.audited(
        "entity_delete", input_payload={"entity_id": entity_id, "existing": row}
    ) as span:
        span.result = _support.execute("entity_delete", {"entity_id": entity_id})
    _support.output(span.result, command="entity-delete", fmt=_support.chosen_format(output_format, json_flag))
