"""Server/admin CLI entrypoint for Tidal."""

from __future__ import annotations

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
from tidal.scan_cli import app as scan_app

app = typer.Typer(help="Tidal server/admin CLI")
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
api_app = typer.Typer(help="API server commands", no_args_is_help=True)

app.add_typer(db_app, name="db")
app.add_typer(scan_app, name="scan")
app.add_typer(auction_app, name="auction")
app.add_typer(kick_app, name="kick")
app.add_typer(logs_app, name="logs")
app.add_typer(api_app, name="api")
app.add_typer(auth_app, name="auth")


@db_app.command("migrate")
def db_migrate(config: ConfigOption = None) -> None:
    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config)
    run_migrations(cli_ctx.settings.database_url)
    typer.echo("migrations applied")


@api_app.command("serve")
def api_serve(config: ConfigOption = None) -> None:
    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config)
    settings = cli_ctx.settings
    uvicorn.run(
        create_app(settings),
        host=settings.tidal_api_host,
        port=settings.tidal_api_port,
        log_level="info",
    )


if __name__ == "__main__":
    app()

