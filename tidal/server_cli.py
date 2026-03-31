"""Server operator CLI entrypoint for Tidal."""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from tidal.api.app import create_app
from tidal.auction_cli import app as auction_app
from tidal.auth_cli import app as auth_app
from tidal.cli_context import CLIContext
from tidal.cli_options import ConfigOption
from tidal.kick_cli import app as kick_app
from tidal.logging import OutputMode, configure_logging
from tidal.logs_cli import app as logs_app
from tidal.migrations import run_migrations
from tidal.resources import read_template_text
from tidal.scan_cli import app as scan_app

app = typer.Typer(help="Tidal server operator CLI")
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
api_app = typer.Typer(help="API server commands", no_args_is_help=True)

app.add_typer(db_app, name="db")
app.add_typer(scan_app, name="scan")
app.add_typer(auction_app, name="auction")
app.add_typer(kick_app, name="kick")
app.add_typer(logs_app, name="logs")
app.add_typer(api_app, name="api")
app.add_typer(auth_app, name="auth")


def _write_template(path: Path, content: str, *, force: bool) -> str:
    if path.exists() and not force:
        return "kept"
    path.write_text(content, encoding="utf-8")
    return "wrote"


@app.command("init-config")
def init_config(
    dest: Path = typer.Option(
        Path("config"),
        "--dest",
        file_okay=False,
        dir_okay=True,
        help="Directory to write tracked server config scaffolds into.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing template files."),
) -> None:
    config_dir = dest.expanduser().resolve()
    config_dir.mkdir(parents=True, exist_ok=True)

    server_path = config_dir / "server.yaml"
    env_example_path = config_dir / ".env.example"

    server_status = _write_template(server_path, read_template_text("server.yaml"), force=force)
    env_status = _write_template(env_example_path, read_template_text("server.env.example"), force=force)

    typer.echo(f"Server config:   {server_path} ({server_status})")
    typer.echo(f"Env example:     {env_example_path} ({env_status})")


@db_app.command("migrate")
def db_migrate(config: ConfigOption = None) -> None:
    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config, mode="server")
    cli_ctx.settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations(cli_ctx.settings.database_url)
    typer.echo("migrations applied")


@api_app.command("serve")
def api_serve(config: ConfigOption = None) -> None:
    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config, mode="server")
    settings = cli_ctx.settings
    uvicorn.run(
        create_app(settings),
        host=settings.tidal_api_host,
        port=settings.tidal_api_port,
        log_level="info",
    )


if __name__ == "__main__":
    app()
