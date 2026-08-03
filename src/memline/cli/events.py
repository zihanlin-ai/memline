"""The async add-processing queue: inspect, retry, ack."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.event_app.command("list")
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
    result = _support.execute_queue("event_list", {"status": status, "limit": page_size, "offset": offset})
    _support.output(result, command="event-list", fmt=_support.chosen_format(output_format, json_flag))


@_support.event_app.command("status")
def event_status(
    event_id: str = typer.Argument(..., help="Event ID returned by an async add."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show one background event, including its result or error."""
    result = _support.execute_queue("event_get", {"event_id": event_id})
    if not result:
        raise click.ClickException(f"Event not found: {event_id}")
    _support.output(result, command="event-status", fmt=_support.chosen_format(output_format, json_flag))


@_support.event_app.command("retry")
def event_retry(
    event_id: str = typer.Argument(..., help="Failed event ID to requeue."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Requeue a failed event with its original input."""
    result = _support.execute_queue("event_retry", {"event_id": event_id})
    _support.output(result, command="event-retry", fmt=_support.chosen_format(output_format, json_flag))


@_support.event_app.command("ack")
def event_ack(
    event_id: Optional[str] = typer.Argument(None, help="Failed event ID to acknowledge."),
    all_: bool = typer.Option(False, "--all", help="Acknowledge all failed events."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("json", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Acknowledge failed events so the CLI warning banner clears."""
    if not event_id and not all_:
        raise typer.BadParameter("Pass an event ID or --all.")
    result = _support.execute_queue("event_ack", {"event_id": None if all_ else event_id})
    _support.output(result, command="event-ack", fmt=_support.chosen_format(output_format, json_flag))
