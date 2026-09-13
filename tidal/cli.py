"""Single local CLI for serving, observation, execution and recovery."""

from __future__ import annotations

from pathlib import Path

import typer

from tidal.auction_cli import app as auction_app
from tidal.kick_cli import app as kick_app
from tidal.logs_cli import app as logs_app
from tidal.server_cli import db_app, api_app, init_config
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


app.command("init")(init_config)


if __name__ == "__main__":
    app()
