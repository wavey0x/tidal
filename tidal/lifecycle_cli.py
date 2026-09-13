"""Small machine-readable operational commands shared by both entry points."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import typer

from tidal.cli_options import ConfigOption, JsonOption
from tidal.cli_renderers import render_status_panel, render_warning_panel
from tidal.config import load_server_settings
from tidal.lifecycle import (
    LifecycleError,
    clear_activation,
    execution_lock,
    inspect_database,
    result,
)
from tidal.paths import default_activation_path, default_db_path, default_txn_lock_path


def emit_operation(operation: Callable[[], dict], *, json_output: bool) -> None:
    try:
        payload = operation()
    except LifecycleError as exc:
        payload = result(exc.code, blockers=[{"code": exc.code, "message": str(exc)}])
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True))
    elif payload["blockers"]:
        render_warning_panel([item["message"] for item in payload["blockers"]])
    else:
        render_status_panel(payload["code"], [f"{key}: {value}" for key, value in payload["data"].items()])
    if payload["blockers"]:
        raise typer.Exit(code=75 if payload["code"] == "BUSY" else 1)


def db_check(
    config: ConfigOption = None,
    database: Path | None = typer.Option(None, "--database", help="Inspect this DB without loading any application configuration."),
    json_output: JsonOption = False,
) -> None:
    """Offline check; never initializes, migrates or contacts any service."""
    def run() -> dict:
        path = database or (load_server_settings(config).resolved_db_path if config else default_db_path())
        return result("OK", data=inspect_database(path))

    emit_operation(run, json_output=json_output)


def hold(json_output: JsonOption = False) -> None:
    """Hold native execution; deployment/restore must also stop existing units."""
    def run() -> dict:
        with execution_lock(default_txn_lock_path()):
            clear_activation(default_activation_path())
            return result("HELD", data={"activated": False})

    emit_operation(run, json_output=json_output)
