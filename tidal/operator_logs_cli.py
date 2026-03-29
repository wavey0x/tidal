"""API-backed log inspection commands."""

from __future__ import annotations

from dataclasses import asdict

import typer

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import ApiBaseUrlOption, ApiKeyOption, AuctionAddressOption, ConfigOption, JsonOption, LimitOption, SourceAddressOption
from tidal.cli_renderers import emit_json, render_kick_logs, render_run_detail, render_scan_runs
from tidal.control_plane.client import ControlPlaneError
from tidal.ops.logs import KickLogRecord, ScanItemErrorRecord, ScanRunDetail, ScanRunRecord, TxnRunDetail

app = typer.Typer(help="Historical log inspection commands", no_args_is_help=True)


def _kick_log_record_from_api(row: dict[str, object]) -> KickLogRecord:
    return KickLogRecord(
        id=int(row["id"]),
        run_id=str(row["runId"]),
        created_at=str(row["createdAt"]),
        operation_type=str(row.get("operationType") or "kick"),
        status=str(row["status"]),
        source_type=str(row["sourceType"]) if row.get("sourceType") is not None else None,
        source_address=str(row["sourceAddress"]) if row.get("sourceAddress") is not None else None,
        auction_address=str(row["auctionAddress"]),
        token_address=str(row["tokenAddress"]),
        token_symbol=str(row["tokenSymbol"]) if row.get("tokenSymbol") is not None else None,
        want_symbol=str(row["wantSymbol"]) if row.get("wantSymbol") is not None else None,
        usd_value=str(row["usdValue"]) if row.get("usdValue") is not None else None,
        error_message=str(row["errorMessage"]) if row.get("errorMessage") is not None else None,
        tx_hash=str(row["txHash"]) if row.get("txHash") is not None else None,
        quote_url=None,
    )


def _scan_run_record_from_api(row: dict[str, object]) -> ScanRunRecord:
    return ScanRunRecord(**row)


def _run_detail_from_api(row: dict[str, object]) -> TxnRunDetail | ScanRunDetail:
    if row["kind"] == "kick":
        return TxnRunDetail(
            kind="kick",
            run_id=row["run_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            candidates_found=row["candidates_found"],
            kicks_attempted=row["kicks_attempted"],
            kicks_succeeded=row["kicks_succeeded"],
            kicks_failed=row["kicks_failed"],
            live=row["live"],
            error_summary=row["error_summary"],
            records=[KickLogRecord(**record) for record in row["records"]],
        )
    return ScanRunDetail(
        kind="scan",
        run_id=row["run_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        vaults_seen=row["vaults_seen"],
        strategies_seen=row["strategies_seen"],
        pairs_seen=row["pairs_seen"],
        pairs_succeeded=row["pairs_succeeded"],
        pairs_failed=row["pairs_failed"],
        error_summary=row["error_summary"],
        errors=[ScanItemErrorRecord(**record) for record in row["errors"]],
    )


@app.command("kicks")
def logs_kicks(
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    json_output: JsonOption = False,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = 20,
    status: str | None = typer.Option(None, "--status", help="Filter by kick status."),
) -> None:
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    normalized_source = normalize_cli_address(source_address)
    normalized_auction = normalize_cli_address(auction_address)
    try:
        with cli_ctx.control_plane_client(auth=False) as client:
            response = client.get_kick_logs(
                limit=limit or 20,
                offset=0,
                status=status,
                source=normalized_source,
                auction=normalized_auction,
            )
    except ControlPlaneError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    data = response["data"]
    if json_output:
        emit_json("logs.kicks", status=response["status"], data=data, warnings=response.get("warnings"))
    else:
        render_kick_logs([_kick_log_record_from_api(item) for item in data["kicks"]])
    raise typer.Exit(code=0 if data["kicks"] else 2)


@app.command("scans")
def logs_scans(
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    json_output: JsonOption = False,
    limit: LimitOption = 20,
    status: str | None = typer.Option(None, "--status", help="Filter by scan status."),
) -> None:
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    try:
        with cli_ctx.control_plane_client(auth=False) as client:
            response = client.get_scan_logs(limit=limit or 20, offset=0, status=status)
    except ControlPlaneError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    data = response["data"]
    if json_output:
        emit_json("logs.scans", status=response["status"], data=data, warnings=response.get("warnings"))
    else:
        render_scan_runs([_scan_run_record_from_api(item) for item in data["items"]])
    raise typer.Exit(code=0 if data["items"] else 2)


@app.command("show")
def logs_show(
    run_id: str = typer.Argument(..., metavar="RUN_ID", help="Run identifier from scan or kick history."),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    json_output: JsonOption = False,
) -> None:
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    try:
        with cli_ctx.control_plane_client(auth=False) as client:
            response = client.get_run_detail(run_id)
    except ControlPlaneError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    detail = response["data"]
    if json_output:
        emit_json("logs.show", status=response["status"], data=detail, warnings=response.get("warnings"))
    else:
        render_run_detail(_run_detail_from_api(detail))

