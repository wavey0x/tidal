"""Scan command group."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

import typer

from tidal.cli_context import CLIContext
from tidal.cli_exit_codes import scan_exit_code
from tidal.cli_options import AutoEnableTokensOption, AutoSettleOption, ConfigOption, JsonOption, NoConfirmationOption
from tidal.cli_validation import require_no_confirmation_for_unattended
from tidal.cli_renderers import emit_json, render_scan_summary
from tidal.errors import ConfigurationError
from tidal.lifecycle import LifecycleError, result as lifecycle_result
from tidal.logging import OutputMode, configure_logging
from tidal.runtime import build_scanner_service

app = typer.Typer(help="Scanner commands", no_args_is_help=True)


def _require_scan_runtime(ctx: CLIContext, *, auto_settle: bool, auto_enable_tokens: bool) -> None:
    ctx.require_rpc()
    if auto_settle or auto_enable_tokens:
        if not ctx.settings.resolved_txn_keystore_path or not ctx.settings.txn_keystore_passphrase:
            raise ConfigurationError("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required for transaction commands")


def _require_scan_confirmation_policy(
    *,
    auto_settle: bool,
    auto_enable_tokens: bool,
    no_confirmation: bool,
) -> None:
    if auto_settle:
        require_no_confirmation_for_unattended(no_confirmation=no_confirmation, command_name="scan auto-settle")
    if auto_enable_tokens:
        require_no_confirmation_for_unattended(no_confirmation=no_confirmation, command_name="scan auto-enable-tokens")


def _run_scan_once(*, ctx: CLIContext, auto_settle: bool, auto_enable_tokens: bool, json_output: bool = False) -> object:
    _require_scan_runtime(ctx, auto_settle=auto_settle, auto_enable_tokens=auto_enable_tokens)
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
        scanner = build_scanner_service(
            ctx.settings,
            session,
            auto_settle=auto_settle,
            auto_enable_tokens=auto_enable_tokens,
        )
        async def run():
            try:
                return await scanner.scan_once(on_progress=None if json_output else show_progress)
            finally:
                if hasattr(scanner, "close"):
                    await scanner.close()
        return asyncio.run(run())


@app.command("run")
def scan_run(
    config: ConfigOption = None,
    json_output: JsonOption = False,
    no_confirmation: NoConfirmationOption = False,
    auto_settle: AutoSettleOption = False,
    auto_enable_tokens: AutoEnableTokensOption = False,
) -> None:
    """Run a single scan cycle."""

    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config, mode="server")
    try:
        _require_scan_confirmation_policy(
            auto_settle=auto_settle,
            auto_enable_tokens=auto_enable_tokens,
            no_confirmation=no_confirmation,
        )
        try:
            cli_ctx.settings = cli_ctx.settings.for_execution_profile("scan")
        except ValueError as exc:
            raise LifecycleError("CONFIGURATION_ERROR", str(exc)) from exc
        result = _run_scan_once(
            ctx=cli_ctx,
            auto_settle=auto_settle,
            auto_enable_tokens=auto_enable_tokens,
            json_output=json_output,
        )
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except LifecycleError as exc:
        if json_output:
            import json
            typer.echo(json.dumps(lifecycle_result(exc.code, blockers=[{"code": exc.code, "message": str(exc)}])))
        else:
            from tidal.cli_renderers import render_warning_panel
            render_warning_panel([str(exc)])
        raise typer.Exit(code=75 if exc.code == "BUSY" else 1) from exc
    except typer.BadParameter as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        emit_json("scan.run", status="ok" if result.status == "SUCCESS" else "error", data=asdict(result))
    else:
        render_scan_summary(result)
    raise typer.Exit(code=scan_exit_code(result.status))
