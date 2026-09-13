"""Stable recovery CLI consumed by restore orchestration and local operators."""
from __future__ import annotations

import asyncio
from pathlib import Path
import typer

from tidal import recovery
from tidal.cli_options import ConfigOption, JsonOption
from tidal.config import load_server_settings
from tidal.lifecycle import LifecycleError
from tidal.lifecycle_cli import emit_operation
from tidal.logging import configure_logging, OutputMode
from tidal.persistence.db import Database
from tidal.transaction_evidence import EvidenceError


def _invoke(config, json_output, operation, *, read_only=False):
    configure_logging(output_mode=OutputMode.JSON if json_output else OutputMode.TEXT)
    def run():
        settings = load_server_settings(config)
        recovery.inspect_database(settings.resolved_db_path)
        database = Database(settings.database_url, read_only=read_only)
        try:
            with database.session() as session:
                value = operation(settings, session)
                return asyncio.run(value) if hasattr(value, "__await__") else value
        except EvidenceError as exc:
            raise LifecycleError(exc.code, str(exc)) from exc
        except LifecycleError:
            raise
        except Exception as exc:
            raise LifecycleError("OPERATION_FAILED", f"Operation could not finish ({type(exc).__name__}); inspect the dependency and retry.") from exc
        finally:
            database.engine.dispose()
    emit_operation(run, json_output=json_output)


def status(config: ConfigOption = None, json_output: JsonOption = False):
    """Read API, chain, price and per-signer readiness; never change state."""
    _invoke(config, json_output, recovery.status, read_only=True)


def reconcile(
    config: ConfigOption = None, json_output: JsonOption = False,
    transaction_id: int | None = typer.Option(None, "--transaction-id", min=1),
    replacement_hash: str | None = typer.Option(None, "--replacement-hash"),
    note: str | None = typer.Option(None, "--note"),
):
    """Check retained identities. Explicit replacement proof never sends."""
    _invoke(config, json_output, lambda settings, session: recovery.reconcile(
        settings, session, transaction_id=transaction_id, replacement_hash=replacement_hash, note=note))


def refresh(
    recovery_mode: bool = typer.Option(False, "--recovery", help="Observe current state without prices, signing or delivery."),
    config: ConfigOption = None, json_output: JsonOption = False,
):
    if not recovery_mode:
        raise typer.BadParameter("Use --recovery for silent current-state refresh, or scan run for normal scanning.")
    _invoke(config, json_output, recovery.refresh_recovery)


def resume(config: ConfigOption = None, json_output: JsonOption = False):
    """Explicitly accept available policy history and activate checked identities."""
    _invoke(config, json_output, recovery.resume)


def prepare_restore(
    credential_file: Path = typer.Option(..., "--credential-file", help="Protected output file for fresh API credentials."),
    config: ConfigOption = None, json_output: JsonOption = False,
):
    """Prepare a restored DB while held: rotate API access and mute old alerts."""
    _invoke(config, json_output, lambda settings, session: recovery.prepare_restore(
        settings, session, credential_file=credential_file.expanduser().resolve()))


def register(app, db_app=None):
    app.command("status")(status)
    app.command("reconcile")(reconcile)
    app.command("refresh")(refresh)
    app.command("resume")(resume)
    if db_app is not None:
        db_app.command("prepare-restore")(prepare_restore)
