"""Scan command group."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

import typer

from tidal.cli_context import CLIContext
from tidal.cli_exit_codes import scan_exit_code
from tidal.cli_options import ConfigOption, IntervalOption, JsonOption
from tidal.cli_renderers import emit_json, render_scan_summary
from tidal.errors import ConfigurationError
from tidal.logging import OutputMode, configure_logging
from tidal.runtime import build_scanner_service

app = typer.Typer(help="Scanner commands", no_args_is_help=True)


def _require_scan_runtime(ctx: CLIContext) -> None:
    ctx.require_rpc()
    if ctx.settings.scan_auto_settle_enabled:
        if not ctx.settings.resolved_txn_keystore_path or not ctx.settings.txn_keystore_passphrase:
            raise ConfigurationError("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required for transaction commands")


def _run_scan_once(*, ctx: CLIContext) -> object:
    _require_scan_runtime(ctx)
    scan_start = time.monotonic()
    step_start = scan_start

    def show_progress(step: int, total: int, label: str, detail: str) -> None:
        nonlocal step_start
        if detail:
            step_elapsed = time.monotonic() - step_start
            total_elapsed = time.monotonic() - scan_start
            typer.echo(
                f"  [{step}/{total}] {label:<28} {detail}  ({step_elapsed:.1f}s / {total_elapsed:.1f}s total)"
            )
            step_start = time.monotonic()

    with ctx.session() as session:
        scanner = build_scanner_service(ctx.settings, session)
        return asyncio.run(scanner.scan_once(on_progress=show_progress))


@app.command("run")
def scan_run(config: ConfigOption = None, json_output: JsonOption = False) -> None:
    """Run a single scan cycle."""

    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config, mode="server")
    try:
        result = _run_scan_once(ctx=cli_ctx)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        emit_json("scan.run", status="ok" if result.status == "SUCCESS" else "error", data=asdict(result))
    else:
        render_scan_summary(result)
    raise typer.Exit(code=scan_exit_code(result.status))


@app.command("daemon")
def scan_daemon(
    config: ConfigOption = None,
    interval_seconds: IntervalOption = None,
    json_output: JsonOption = False,
) -> None:
    """Run the scanner continuously."""

    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config, mode="server")
    try:
        _require_scan_runtime(cli_ctx)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    sleep_seconds = interval_seconds or cli_ctx.settings.scan_interval_seconds

    async def run_loop() -> None:
        while True:
            with cli_ctx.session() as session:
                scanner = build_scanner_service(cli_ctx.settings, session)
                result = await scanner.scan_once()
            if json_output:
                emit_json("scan.daemon", status="ok" if result.status == "SUCCESS" else "error", data=asdict(result))
            else:
                render_scan_summary(result)
            await asyncio.sleep(sleep_seconds)

    asyncio.run(run_loop())
