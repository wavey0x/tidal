"""Server runtime CLI entrypoint for Tidal."""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from tidal.api.app import create_app
from tidal.auction_rounds import plan_no_fill_suspension_clear
from tidal.auth_cli import app as auth_app
from tidal.auction_round_repair import AuctionRoundRepair
from tidal.cli_renderers import render_status_panel, render_warning_panel
from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import ConfigOption, JsonOption
from tidal.logging import OutputMode, configure_logging
from tidal.migrations import run_migrations
from tidal.lifecycle import clear_activation, execution_lock
from tidal.lifecycle_cli import db_check, db_import_legacy, db_snapshot, hold, emit_operation
from tidal.paths import default_activation_path, default_txn_lock_path
from tidal.persistence.db import Database
from tidal.persistence.repositories import KickTxRepository
from tidal.runtime import build_web3_client
from tidal.resources import read_template_text
from tidal.scan_cli import app as scan_app
from tidal.time import utcnow_iso

app = typer.Typer(help="Tidal server runtime CLI")
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
api_app = typer.Typer(help="API server commands", no_args_is_help=True)

app.add_typer(db_app, name="db")
app.add_typer(scan_app, name="scan")
app.add_typer(api_app, name="api")
app.add_typer(auth_app, name="auth")
db_app.command("check")(db_check)
db_app.command("snapshot")(db_snapshot)
db_app.command("import-legacy")(db_import_legacy)
app.command("hold")(hold)
from tidal.recovery_cli import register as register_recovery
register_recovery(app, db_app)


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
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing template files."
    ),
) -> None:
    config_dir = dest.expanduser().resolve()
    config_dir.mkdir(parents=True, exist_ok=True)

    server_path = config_dir / "server.yaml"
    env_example_path = config_dir / ".env.example"

    server_status = _write_template(
        server_path, read_template_text("server.yaml"), force=force
    )
    env_status = _write_template(
        env_example_path, read_template_text("server.env.example"), force=force
    )

    typer.echo(f"Server config:   {server_path} ({server_status})")
    typer.echo(f"Env example:     {env_example_path} ({env_status})")


@db_app.command("migrate")
def db_migrate(
    config: ConfigOption = None,
    source_database: Path | None = typer.Option(None, "--source-database", help="Protected original pre-consolidation DB."),
    outbox: Path | None = typer.Option(None, "--outbox", help="Protected matching original operator outbox."),
    json_output: JsonOption = False,
) -> None:
    from tidal.migrate_state import migrate_state
    configure_logging(output_mode=OutputMode.JSON if json_output else OutputMode.TEXT)
    emit_operation(lambda: migrate_state(CLIContext(config).settings,
        source_database=source_database, outbox=outbox), json_output=json_output)


@db_app.command("init")
def db_init(config: ConfigOption = None) -> None:
    """Explicitly initialize empty state. Execution remains held."""
    configure_logging(output_mode=OutputMode.TEXT)
    settings = CLIContext(config).settings
    with execution_lock(default_txn_lock_path()):
        if settings.resolved_db_path.exists():
            raise typer.BadParameter("Database already exists; inspect or migrate it explicitly.")
        clear_activation(default_activation_path())
        settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
        run_migrations(settings.database_url)
    typer.echo("database initialized; execution held")


@db_app.command("repair-auction-rounds")
def db_repair_auction_rounds(
    auction: str = typer.Option(..., "--auction", help="Exact auction to review."),
    token: str = typer.Option(..., "--token", help="Exact sell token to review."),
    kick_id: int | None = typer.Option(None, "--kick-id", min=1, help="Confirmed kick for an exact settlement repair."),
    settlement_tx: str | None = typer.Option(None, "--settlement-tx", help="Verify this settlement transaction instead of searching logs; requires --kick-id."),
    config: ConfigOption = None,
    json_output: JsonOption = False,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Repair this pair's evidence and baseline its reviewed inactive history gaps.",
    ),
) -> None:
    import asyncio

    if (kick_id is None) != (settlement_tx is None):
        raise typer.BadParameter("Provide both --kick-id and --settlement-tx.")
    if settlement_tx is not None:
        import re
        if re.fullmatch(r"0x[0-9a-fA-F]{64}", settlement_tx) is None:
            raise typer.BadParameter("--settlement-tx must be a 0x-prefixed 32-byte transaction hash.")

    configure_logging(output_mode=OutputMode.JSON if json_output else OutputMode.TEXT)
    cli_ctx = CLIContext(config)
    settings = cli_ctx.settings
    database = Database(settings.database_url)
    web3_client = build_web3_client(settings)

    async def _run():  # noqa: ANN202
        try:
            with database.session() as session:
                return await AuctionRoundRepair(
                    session=session,
                    settings=settings,
                    web3_client=web3_client,
                ).run(auction=auction, token=token, apply=apply, kick_id=kick_id, settlement_tx_hash=settlement_tx)
        finally:
            await web3_client.close()
            database.engine.dispose()

    def run():
        from dataclasses import asdict
        from tidal.lifecycle import result
        report = asyncio.run(_run())
        return result("REPAIRED" if apply and report.passed else "REVIEW", data=asdict(report),
            blockers=[] if report.passed else [{"code": "HISTORY_REVIEW_REQUIRED", "message": "Inspect this pair's evidence and proposed baseline IDs before applying a repair."}])
    emit_operation(run, json_output=json_output)


@db_app.command("clear-no-fill-suspension")
def db_clear_no_fill_suspension(
    auction: str = typer.Option(..., "--auction", help="Auction contract address."),
    token: str = typer.Option(..., "--token", help="Sell token address."),
    config: ConfigOption = None,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Persist the reviewed baseline. Without this flag the command is read-only.",
    ),
) -> None:
    configure_logging(output_mode=OutputMode.TEXT)
    auction_address = normalize_cli_address(auction, param_hint="--auction")
    token_address = normalize_cli_address(token, param_hint="--token")
    assert auction_address is not None and token_address is not None

    cli_ctx = CLIContext(config)
    database = Database(cli_ctx.settings.database_url)
    with execution_lock(cli_ctx.settings.resolved_home_path / "execution.lock"), database.session() as session:
        repo = KickTxRepository(session)
        try:
            plan = plan_no_fill_suspension_clear(
                repo.list_pair_operations(auction_address, token_address)
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if apply:
            from tidal.auction_round_repair import assert_history_repair_unblocked
            assert_history_repair_unblocked(session)
            repo.update_fields(
                plan.baseline_kick_id,
                historical_baseline=1,
                historical_baseline_reason="OPERATOR_CLEARED_NO_FILL_SUSPENSION",
                historical_baselined_at=utcnow_iso(),
            )
            session.commit()

    lines = [
        f"Auction: {auction_address}",
        f"Token: {token_address}",
        f"Baseline kick: {plan.baseline_kick_id}",
    ]
    if plan.newer_kick_ids:
        lines.append(
            "Newer rounds remain enforced: "
            + ", ".join(str(kick_id) for kick_id in plan.newer_kick_ids)
        )
    lines.append("Status: applied" if apply else "Status: preview only")
    render_status_panel(
        "Clear no-fill suspension",
        lines,
        border_style="green" if apply else "cyan",
    )
    if not apply:
        render_warning_panel(["Re-run with --apply to persist this baseline."])


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
