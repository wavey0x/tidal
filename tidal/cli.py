"""Operator CLI entrypoint for API-backed Tidal commands."""

from __future__ import annotations

from pathlib import Path

import typer

from tidal.operator_auction_cli import app as auction_app
from tidal.operator_kick_cli import app as kick_app
from tidal.operator_logs_cli import app as logs_app
from tidal.paths import (
    default_action_outbox_path,
    default_config_path,
    default_db_path,
    default_env_path,
    default_operator_state_dir,
    default_run_dir,
    default_state_dir,
    default_txn_lock_path,
    tidal_home,
)
from tidal.resources import read_template_text

app = typer.Typer(help="Tidal CLI client")

app.add_typer(auction_app, name="auction")
app.add_typer(kick_app, name="kick")
app.add_typer(logs_app, name="logs")


def _write_template(path: Path, content: str, *, force: bool) -> str:
    if path.exists() and not force:
        return "kept"
    path.write_text(content, encoding="utf-8")
    return "wrote"


@app.command("init")
def init_command(
    force: bool = typer.Option(False, "--force", help="Overwrite existing template files."),
) -> None:
    home_dir = tidal_home()
    state_dir = default_state_dir()
    operator_state_dir = default_operator_state_dir()
    run_dir = default_run_dir()

    for directory in (home_dir, state_dir, operator_state_dir, run_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = default_config_path()
    env_path = default_env_path()
    config_status = _write_template(config_path, read_template_text("config.yaml"), force=force)
    env_status = _write_template(env_path, read_template_text("env.template"), force=force)

    typer.echo(f"Home:            {home_dir}")
    typer.echo(f"Config:          {config_path} ({config_status})")
    typer.echo(f"Env:             {env_path} ({env_status})")
    typer.echo(f"Database:        {default_db_path()}")
    typer.echo(f"Outbox:          {default_action_outbox_path()}")
    typer.echo(f"Lock file:       {default_txn_lock_path()}")


if __name__ == "__main__":
    app()
