"""API key management commands."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

import typer
from sqlalchemy import select, update

from tidal.cli_context import CLIContext
from tidal.cli_options import ConfigOption
from tidal.persistence import models

app = typer.Typer(help="API key management", no_args_is_help=True)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.command("create")
def auth_create(
    label: str = typer.Option(..., "--label", help="Unique operator label for this key."),
    config: ConfigOption = None,
) -> None:
    """Create a new API key for an operator."""
    cli_ctx = CLIContext(config, mode="server")
    with cli_ctx.session() as session:
        existing = session.execute(
            select(models.api_keys.c.label).where(models.api_keys.c.label == label)
        ).first()
        if existing is not None:
            typer.echo(f"Label already exists: {label}", err=True)
            raise typer.Exit(code=1)

        raw_key = secrets.token_urlsafe(32)
        session.execute(
            models.api_keys.insert().values(
                label=label,
                key_hash=_hash_key(raw_key),
                key_prefix=raw_key[:8],
                created_at=_now_iso(),
            )
        )
        session.commit()

    typer.echo(f"Created API key for '{label}'.")
    typer.echo(f"Key: {raw_key}")
    typer.echo("Store this key now — it cannot be retrieved again.")


@app.command("list")
def auth_list(
    config: ConfigOption = None,
) -> None:
    """List all API keys."""
    cli_ctx = CLIContext(config, mode="server")
    with cli_ctx.session() as session:
        rows = session.execute(
            select(
                models.api_keys.c.label,
                models.api_keys.c.key_prefix,
                models.api_keys.c.created_at,
                models.api_keys.c.revoked_at,
            ).order_by(models.api_keys.c.created_at)
        ).all()

    if not rows:
        typer.echo("No API keys found.")
        return

    typer.echo(f"{'LABEL':<20} {'PREFIX':<12} {'STATUS':<10} {'CREATED'}")
    typer.echo("-" * 72)
    for label, prefix, created_at, revoked_at in rows:
        status = "revoked" if revoked_at else "active"
        typer.echo(f"{label:<20} {prefix + '…':<12} {status:<10} {created_at}")


@app.command("revoke")
def auth_revoke(
    label: str = typer.Argument(..., help="Label of the key to revoke."),
    config: ConfigOption = None,
) -> None:
    """Revoke an API key by label."""
    cli_ctx = CLIContext(config, mode="server")
    with cli_ctx.session() as session:
        row = session.execute(
            select(models.api_keys.c.label, models.api_keys.c.revoked_at).where(
                models.api_keys.c.label == label
            )
        ).first()

        if row is None:
            typer.echo(f"No key found for label: {label}", err=True)
            raise typer.Exit(code=1)

        if row.revoked_at is not None:
            typer.echo(f"Key already revoked: {label}", err=True)
            raise typer.Exit(code=1)

        session.execute(
            update(models.api_keys)
            .where(models.api_keys.c.label == label)
            .values(revoked_at=_now_iso())
        )
        session.commit()

    typer.echo(f"Revoked key for '{label}'.")
