"""Local operator commands using the same services as scheduled execution."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict

import typer
from sqlalchemy import select

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import (
    AuctionAddressOption, ConfigOption, HeadlessOption, JsonOption, KeystoreOption,
    LimitOption, MinUsdValueOption, NoConfirmationOption, PasswordFileOption,
    SourceAddressOption, SourceTypeOption, TokenAddressOption, VerboseOption,
)
from tidal.cli_renderers import (
    render_kick_inspect, render_kick_run_summary, render_kick_submission_summary,
    render_status_panel, render_warning_panel,
)
from tidal.cli_validation import require_no_confirmation_for_json
from tidal.lifecycle import LifecycleError, result
from tidal.lifecycle_cli import emit_operation
from tidal.logging import OutputMode, configure_logging
from tidal.ops.kick_inspect import inspect_kick_candidates
from tidal.persistence import models
from tidal.runtime import build_txn_service
from tidal.transactions import TransactionRepository

app = typer.Typer(help="Inspect and execute kicks locally on the application host", no_args_is_help=True)


def _normalize_source_type_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"strategy", "fee_burner"}:
        raise typer.BadParameter("expected 'strategy' or 'fee-burner'", param_hint="--source-type")
    return normalized


def _profile_settings(settings, source_type: str, *, min_usd_value=None, max_base_fee_gwei=None, require_curve_quote=None):
    try:
        effective = settings.for_execution_profile(source_type)
    except ValueError as exc:
        raise LifecycleError("CONFIGURATION_ERROR", str(exc)) from exc
    overrides = {
        "txn_usd_threshold": min_usd_value, "txn_base_fee_cap_gwei": max_base_fee_gwei,
        "txn_require_curve_quote": require_curve_quote,
    }
    return effective.model_copy(update={key: value for key, value in overrides.items() if value is not None})


@app.command("inspect")
def kick_inspect(
    config: ConfigOption = None, json_output: JsonOption = False,
    source_type: SourceTypeOption = None, source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None, token_address: TokenAddressOption = None,
    limit: LimitOption = None, min_usd_value: MinUsdValueOption = None,
    show_all: bool = typer.Option(False, "--show-all", help="Show deferred and limited candidates."),
) -> None:
    """Read current candidates locally; no signer or API credentials required."""
    ctx = CLIContext(config, mode="server")
    selected = _normalize_source_type_filter(source_type)
    def inspect() -> dict:
        profiles = [selected] if selected else ["strategy", "fee_burner"]
        data = {}
        with ctx.session() as session:
            for profile in profiles:
                effective = _profile_settings(ctx.settings, profile, min_usd_value=min_usd_value)
                found = inspect_kick_candidates(
                    session, effective, source_type=profile,
                    source_address=normalize_cli_address(source_address),
                    auction_address=normalize_cli_address(auction_address),
                    token_address=normalize_cli_address(token_address), limit=limit,
                )
                data[profile] = asdict(found)
                if not json_output:
                    render_kick_inspect(found, show_all=show_all)
        return result("OK", data=data)
    emit_operation(inspect, json_output=json_output)


@app.command("run")
def kick_run(
    config: ConfigOption = None, json_output: JsonOption = False,
    no_confirmation: NoConfirmationOption = False, headless: HeadlessOption = False,
    source_type: SourceTypeOption = None, source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None, token_address: TokenAddressOption = None,
    limit: LimitOption = None, min_usd_value: MinUsdValueOption = None,
    keystore: KeystoreOption = None, password_file: PasswordFileOption = None,
    verbose: VerboseOption = False,
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepare and record diagnostics without unlocking or sending."),
    batch: bool = typer.Option(False, "--batch/--no-batch", help="Combine compatible kicks into one transaction."),
    max_base_fee_gwei: float | None = typer.Option(None, "--max-base-fee-gwei", min=0),
    require_curve_quote: bool | None = typer.Option(None, "--require-curve/--no-require-curve"),
    allow_killed_gauge: bool = typer.Option(False, "--allow-killed-gauge"),
    allow_no_fill_retry: bool = typer.Option(False, "--allow-no-fill-retry"),
) -> None:
    """Prepare and execute locally under the shared lock; retained attempts gate sends."""
    unattended = no_confirmation or headless
    require_no_confirmation_for_json(json_output=json_output and not dry_run, no_confirmation=unattended)
    selected = _normalize_source_type_filter(source_type)
    auction = normalize_cli_address(auction_address, param_hint="--auction")
    token = normalize_cli_address(token_address, param_hint="--token")
    source = normalize_cli_address(source_address, param_hint="--source")
    if allow_no_fill_retry and (not auction or not token):
        raise typer.BadParameter("--allow-no-fill-retry requires both --auction and --token")
    if allow_no_fill_retry and headless:
        raise typer.BadParameter("--allow-no-fill-retry cannot be used with --headless")
    configure_logging(output_mode=OutputMode.JSON if json_output else OutputMode.TEXT)
    ctx = CLIContext(config, mode="server")

    def confirm(summary):
        if summary.get("kicks"):
            render_kick_submission_summary(summary)
        else:
            render_status_panel("Resolve auction", [str(summary)], border_style="cyan")
        return typer.confirm("Submit this transaction?", default=False)

    async def execute() -> dict:
        ctx.require_rpc()
        # Explicit native construction is the only place a CLI signer is
        # unlocked. Preview composition never discovers or decrypts keys.
        execution = ctx.resolve_execution(
            required=not dry_run, required_for="local kick execution",
            keystore_path=keystore, password_file=password_file,
        ) if not dry_run else None
        profiles = [selected] if selected else ["strategy", "fee_burner"]
        runs = []
        with ctx.session() as session:
            for profile in profiles:
                effective = _profile_settings(
                    ctx.settings, profile, min_usd_value=min_usd_value,
                    max_base_fee_gwei=max_base_fee_gwei, require_curve_quote=require_curve_quote,
                )
                async with AsyncExitStack() as clients:
                    service = build_txn_service(
                        effective, session, signer=execution.signer if execution else None,
                        confirm_fn=None if unattended or dry_run else confirm, owned_clients=clients,
                    )
                    try:
                        outcome = await service.run_once(
                            live=not dry_run, batch=batch, source_type=profile,
                            source_address=source, auction_address=auction, token_address=token,
                            limit=limit, allow_no_fill_retry=allow_no_fill_retry,
                            allow_killed_gauge=allow_killed_gauge,
                        )
                    except LifecycleError as exc:
                        if runs:
                            return result(exc.code, data={"runs": runs}, blockers=[{"code": exc.code, "message": str(exc)}])
                        raise
                    session.commit()
                    runs.append(asdict(outcome))
                    if not json_output:
                        rows = [dict(row) for row in session.execute(select(models.kick_txs).where(
                            models.kick_txs.c.run_id == outcome.run_id,
                        )).mappings()]
                        render_kick_run_summary(
                            result=outcome, live=not dry_run, source_type=profile, source_address=source,
                            auction_address=auction, run_rows=rows, verbose=verbose,
                            sender=execution.sender if execution else None,
                        )
            pending = TransactionRepository(session).unresolved()
        waiting_runs = [run for run in runs if run["status"] in {"BUSY", "WAITING"}]
        code = "WAITING" if pending or waiting_runs else "OK"
        blockers = [{"code": "UNRESOLVED_ATTEMPTS", "message": f"{len(pending)} retained attempt(s) await reconciliation."}] if pending else []
        blockers.extend({"code": run["status"], "message": "Another command owns execution; retry later." if run["status"] == "BUSY"
                         else "; ".join((run.get("failure_summary") or {"Execution is held for review": 1}).keys())}
                        for run in waiting_runs)
        if any(run["kicks_failed"] for run in runs):
            code = "EXECUTION_ERROR"
            blockers.append({"code": code, "message": "One or more operations failed; inspect the retained run details."})
        return result(code, data={"runs": runs, "pending_transactions": len(pending)}, blockers=blockers)

    emit_operation(lambda: asyncio.run(execute()), json_output=json_output)


if __name__ == "__main__":
    app()
