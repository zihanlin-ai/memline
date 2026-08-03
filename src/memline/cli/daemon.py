"""Daemon lifecycle commands."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.daemon_app.command("start")
def daemon_start(
    wait_seconds: float = typer.Option(90.0, "--wait", help="Seconds to wait for daemon readiness."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Start the optional local daemon."""
    from memline.daemon import start_daemon

    result = start_daemon(wait_seconds=wait_seconds)
    _support.output(result, command="daemon-start", fmt=_support.chosen_format(output_format, json_flag))


@_support.daemon_app.command("stop")
def daemon_stop(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Stop the optional local daemon."""
    from memline.daemon import stop_daemon

    result = stop_daemon()
    _support.output(result, command="daemon-stop", fmt=_support.chosen_format(output_format, json_flag))


@_support.daemon_app.command("status")
def daemon_status(
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Show optional local daemon status."""
    from memline.daemon import status as daemon_status_data

    result = daemon_status_data()
    _support.output(result, command="daemon-status", fmt=_support.chosen_format(output_format, json_flag))
