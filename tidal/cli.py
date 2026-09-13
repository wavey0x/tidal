"""Operator CLI entrypoint for API-backed Tidal commands."""

from __future__ import annotations

from pathlib import Path

import typer

from tidal.auction_cli import app as auction_app
from tidal.kick_cli import app as kick_app
from tidal.logs_cli import app as logs_app
from tidal.paths import (
    default_cli_dir,
    default_config_path,
    default_env_path,
    tidal_home,
)
from tidal.resources import read_template_text
from tidal.server_cli import db_app, api_app
from tidal.scan_cli import app as scan_app
from tidal.auth_cli import app as auth_app
from tidal.lifecycle_cli import hold

app = typer.Typer(help="Tidal operator CLI")

app.add_typer(auction_app, name="auction")
app.add_typer(kick_app, name="kick")
app.add_typer(logs_app, name="logs")
app.add_typer(db_app, name="db")
app.add_typer(api_app, name="api")
app.add_typer(scan_app, name="scan")
app.add_typer(auth_app, name="auth")
app.command("hold")(hold)
from tidal.recovery_cli import register as register_recovery
register_recovery(app)


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
    cli_dir = default_cli_dir()

    for directory in (home_dir, cli_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_path = default_config_path()
    env_path = default_env_path()
    config_status = _write_template(config_path, read_template_text("config.yaml"), force=force)
    env_status = _write_template(env_path, read_template_text("env.template"), force=force)

    typer.echo(f"Home:            {home_dir}")
    typer.echo(f"Client dir:      {cli_dir}")
    typer.echo(f"Config:          {config_path} ({config_status})")
    typer.echo(f"Env:             {env_path} ({env_status})")


if __name__ == "__main__":
    app()
