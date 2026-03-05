"""CLI entrypoint for tidal."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from tidal.config import load_settings
from tidal.errors import ConfigurationError
from tidal.health import run_healthcheck
from tidal.logging import configure_logging
from tidal.migrations import run_migrations
from tidal.persistence.db import Database
from tidal.runtime import build_scanner_service, build_web3_client

app = typer.Typer(help="Tidal scanner CLI")
db_app = typer.Typer(help="Database commands")
scan_app = typer.Typer(help="Scanner commands")

app.add_typer(db_app, name="db")
app.add_typer(scan_app, name="scan")


def _require_rpc_url(settings) -> None:
    if not settings.rpc_url:
        raise ConfigurationError("RPC_URL is required for this command")


@db_app.command("migrate")
def db_migrate(
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run Alembic migrations to create/update schema."""

    configure_logging()
    settings = load_settings(config)
    run_migrations(settings.database_url)
    typer.echo("migrations applied")


@scan_app.command("once")
def scan_once(
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run a single scan cycle."""

    configure_logging()
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    db = Database(settings.database_url)
    with db.session() as session:
        scanner = build_scanner_service(settings, session)
        result = asyncio.run(scanner.scan_once())
        typer.echo(
            (
                f"scan_complete run_id={result.run_id} status={result.status} "
                f"strategies={result.strategies_seen} pairs={result.pairs_seen} "
                f"succeeded={result.pairs_succeeded} failed={result.pairs_failed}"
            )
        )


@scan_app.command("daemon")
def scan_daemon(
    interval_seconds: int | None = typer.Option(default=None, min=1),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run the scanner continuously."""

    configure_logging()
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    sleep_seconds = interval_seconds or settings.scan_interval_seconds

    async def _run() -> None:
        while True:
            db = Database(settings.database_url)
            with db.session() as session:
                scanner = build_scanner_service(settings, session)
                result = await scanner.scan_once()
                typer.echo(
                    (
                        f"scan_complete run_id={result.run_id} status={result.status} "
                        f"pairs={result.pairs_seen} succeeded={result.pairs_succeeded} "
                        f"failed={result.pairs_failed}"
                    )
                )
            await asyncio.sleep(sleep_seconds)

    asyncio.run(_run())


@app.command("healthcheck")
def healthcheck(
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Check database and RPC connectivity."""

    configure_logging()
    settings = load_settings(config)

    db = Database(settings.database_url)
    with db.session() as session:
        web3_client = None
        if settings.rpc_url:
            web3_client = build_web3_client(settings)

        result = asyncio.run(run_healthcheck(session, web3_client))

    typer.echo(result)


if __name__ == "__main__":
    app()
