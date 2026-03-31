"""Log inspection command group."""

from __future__ import annotations

from dataclasses import asdict

import typer

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_exit_codes import simple_list_exit_code
from tidal.cli_options import AuctionAddressOption, ConfigOption, JsonOption, LimitOption, SourceAddressOption
from tidal.cli_renderers import emit_json, render_kick_logs, render_run_detail, render_scan_runs
from tidal.ops.logs import get_run_detail, list_kick_logs, list_scan_runs

app = typer.Typer(help="Historical log inspection commands", no_args_is_help=True)


@app.command("kicks")
def logs_kicks(
    config: ConfigOption = None,
    json_output: JsonOption = False,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = 20,
    status: str | None = typer.Option(None, "--status", help="Filter by kick status."),
) -> None:
    """Show recent kick attempts."""

    cli_ctx = CLIContext(config, mode="server")
    normalized_source = normalize_cli_address(source_address)
    normalized_auction = normalize_cli_address(auction_address)
    with cli_ctx.session() as session:
        records = list_kick_logs(
            session,
            source_address=normalized_source,
            auction_address=normalized_auction,
            status=status,
            limit=limit or 20,
        )

    if json_output:
        emit_json("logs.kicks", status="ok" if records else "noop", data=[asdict(record) for record in records])
    else:
        render_kick_logs(records)
    raise typer.Exit(code=simple_list_exit_code(len(records)))


@app.command("scans")
def logs_scans(
    config: ConfigOption = None,
    json_output: JsonOption = False,
    limit: LimitOption = 20,
    status: str | None = typer.Option(None, "--status", help="Filter by scan status."),
) -> None:
    """Show recent scan runs."""

    cli_ctx = CLIContext(config, mode="server")
    with cli_ctx.session() as session:
        records = list_scan_runs(session, status=status, limit=limit or 20)

    if json_output:
        emit_json("logs.scans", status="ok" if records else "noop", data=[asdict(record) for record in records])
    else:
        render_scan_runs(records)
    raise typer.Exit(code=simple_list_exit_code(len(records)))


@app.command("show")
def logs_show(
    run_id: str = typer.Argument(..., metavar="RUN_ID", help="Run identifier from scan or kick history."),
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> None:
    """Show one scan or kick run in detail."""

    cli_ctx = CLIContext(config, mode="server")
    with cli_ctx.session() as session:
        detail = get_run_detail(session, run_id)

    if detail is None:
        typer.echo(f"No run found for {run_id}", err=True)
        raise typer.Exit(code=2)

    if json_output:
        emit_json("logs.show", status="ok", data=asdict(detail))
    else:
        render_run_detail(detail)
