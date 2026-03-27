"""CLI entrypoint for tidal."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path

import structlog
import typer
from sqlalchemy import select

from tidal.config import load_settings
from tidal.errors import ConfigurationError
from tidal.health import run_healthcheck
from tidal.logging import OutputMode, configure_logging
from tidal.migrations import run_migrations
from tidal.normalizers import short_address
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.runtime import build_scanner_service, build_txn_service, build_web3_client
from tidal.transaction_service.types import SourceType

logger = structlog.get_logger(__name__)

app = typer.Typer(help="Tidal scanner CLI")
db_app = typer.Typer(help="Database commands")
scan_app = typer.Typer(help="Scanner commands", invoke_without_command=True)
txn_app = typer.Typer(help="Transaction service commands", invoke_without_command=True)

app.add_typer(db_app, name="db")
app.add_typer(scan_app, name="scan")
app.add_typer(txn_app, name="txn")


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


def _run_scan_once(*, config: Path | None) -> None:
    """Execute a single scan cycle."""

    configure_logging()
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if settings.scan_auto_settle_enabled:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    import sys
    import time

    _scan_start = time.monotonic()
    _step_start = _scan_start

    def _show_progress(step: int, total: int, label: str, detail: str) -> None:
        nonlocal _step_start
        if detail:
            step_elapsed = time.monotonic() - _step_start
            total_elapsed = time.monotonic() - _scan_start
            sys.stdout.write(
                f"\r  [{step}/{total}] {label:<28} {detail}  ({step_elapsed:.1f}s / {total_elapsed:.1f}s total)\n"
            )
            _step_start = time.monotonic()
        else:
            sys.stdout.write(f"\r  [{step}/{total}] {label}...")
        sys.stdout.flush()

    db = Database(settings.database_url)
    with db.session() as session:
        scanner = build_scanner_service(settings, session)
        result = asyncio.run(scanner.scan_once(on_progress=_show_progress))
        elapsed = time.monotonic() - _scan_start
        typer.echo(
            (
                f"scan_complete run_id={result.run_id} status={result.status} "
                f"strategies={result.strategies_seen} pairs={result.pairs_seen} "
                f"succeeded={result.pairs_succeeded} failed={result.pairs_failed} "
                f"elapsed={elapsed:.1f}s"
            )
        )


@scan_app.callback()
def scan(
    ctx: typer.Context,
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run a single scan cycle."""
    if ctx.invoked_subcommand is not None:
        return
    _run_scan_once(config=config)


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
        if settings.scan_auto_settle_enabled:
            _require_keystore(settings)
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


def _require_keystore(settings) -> None:
    if not settings.txn_keystore_path or not settings.txn_keystore_passphrase:
        raise ConfigurationError("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE are required for txn commands")


def _normalize_source_type_filter(value: str | None) -> SourceType | None:
    if value is None:
        return None

    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"strategy", "fee_burner"}:
        return normalized  # type: ignore[return-value]

    raise typer.BadParameter("expected 'strategy' or 'fee-burner'")


def _display_source_type_filter(source_type: SourceType | None) -> str:
    if source_type is None:
        return ""
    return source_type.replace("_", "-")


def _resolve_txn_output_mode(
    *,
    requested: OutputMode | None,
    confirm: bool,
    daemon: bool = False,
) -> OutputMode:
    if requested is not None:
        return requested
    if daemon:
        return OutputMode.JSON
    if confirm or (sys.stdin.isatty() and sys.stdout.isatty()):
        return OutputMode.TEXT
    return OutputMode.JSON


def _txn_scope_label(source_type: SourceType | None) -> str:
    source_label = _display_source_type_filter(source_type)
    return f"{source_label} candidates" if source_label else "candidates"


def _load_run_rows(session, run_id: str) -> list[dict[str, object]]:
    stmt = (
        select(models.kick_txs)
        .where(models.kick_txs.c.run_id == run_id)
        .order_by(models.kick_txs.c.id.asc())
    )
    return [dict(row) for row in session.execute(stmt).mappings().all()]


def _echo_txn_text_summary(
    *,
    result,
    live: bool,
    source_type: SourceType | None,
    run_rows: list[dict[str, object]],
    verbose: bool,
) -> None:
    statuses = {str(row["status"]) for row in run_rows}
    tx_hashes = [str(row["tx_hash"]) for row in run_rows if row.get("tx_hash")]
    skipped_count = sum(1 for row in run_rows if str(row.get("status")) == "USER_SKIPPED")
    type_label = _display_source_type_filter(source_type)
    eligible_candidates_found = getattr(result, "eligible_candidates_found", None)
    deferred_same_auction_count = getattr(result, "deferred_same_auction_count", 0)

    if skipped_count and skipped_count == len(run_rows):
        typer.echo("Skipped by operator. No transaction sent.")
    elif result.candidates_found == 0:
        typer.echo("No eligible candidates.")
    elif not live:
        typer.echo("Dry run complete.")
    elif result.kicks_failed and result.kicks_succeeded:
        typer.echo("Completed with failures.")
    elif "CONFIRMED" in statuses:
        typer.echo("Confirmed.")
    elif "SUBMITTED" in statuses:
        typer.echo("Submitted.")
    elif result.kicks_failed:
        typer.echo("Failed.")
    else:
        typer.echo("Completed.")

    if tx_hashes:
        typer.echo(f"Tx hash:      {tx_hashes[0]}")

    typer.echo(f"Run ID:       {result.run_id}")
    if type_label:
        typer.echo(f"Type:         {type_label}")
    if eligible_candidates_found is not None and eligible_candidates_found != result.candidates_found:
        typer.echo(f"Eligible:     {eligible_candidates_found}")
    typer.echo(f"Candidates:   {result.candidates_found}")
    if live:
        typer.echo(f"Attempted:    {result.kicks_attempted}")
        typer.echo(f"Succeeded:    {result.kicks_succeeded}")
        typer.echo(f"Failed:       {result.kicks_failed}")
        if skipped_count:
            typer.echo(f"Skipped:      {skipped_count}")
    else:
        typer.echo(f"Would kick:   {result.kicks_attempted}")

    if deferred_same_auction_count:
        typer.echo(f"Deferred:     {deferred_same_auction_count}")
        typer.echo("Note:         only one lot per auction can be kicked at a time; deferred tokens stay pending for later runs.")

    if verbose and result.failure_summary:
        typer.echo("Failure summary:")
        for message, count in sorted(result.failure_summary.items(), key=lambda item: (-item[1], item[0])):
            typer.echo(f"  {count}x {message}")


def _make_confirm_fn() -> Callable[[dict], bool]:
    """Return a confirm callback that displays a batch summary."""

    def _confirm_batch(summary: dict) -> bool:
        kicks = summary["kicks"]
        batch_size = summary["batch_size"]
        gas_cost_eth = summary.get("gas_cost_eth", 0)
        priority_fee = summary.get("priority_fee_gwei", 0)
        max_fee = summary.get("max_fee_per_gas_gwei", 0)
        gas_estimate = summary.get("gas_estimate", 0)

        if batch_size == 1:
            # Single-kick display (preserves old UX).
            k = kicks[0]
            source_name = k.get("source_name") or "Unknown"
            token_sym = k.get("token_symbol") or "???"
            want_sym = k.get("want_symbol") or "???"
            profile_name = k.get("pricing_profile_name") or "default"
            amount = float(k["sell_amount"])
            amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"
            quote_amount = Decimal(str(k["quote_amount"]))
            usd_value = Decimal(str(k["usd_value"]))

            starting_price = int(k["starting_price"])
            minimum_price = int(k["minimum_price"])
            step_decay_rate_bps = k.get("step_decay_rate_bps")
            step_decay_str = (
                f"{step_decay_rate_bps / 100:.2f}%"
                if step_decay_rate_bps is not None
                else "—"
            )
            quote_amount_str = f"{float(quote_amount):,.4f}" if quote_amount < 1 else f"{float(quote_amount):,.2f}"
            quote_value_line = None
            divergence_line = None
            want_price_usd = k.get("want_price_usd")
            if want_price_usd is not None:
                try:
                    want_price = Decimal(str(want_price_usd))
                    quote_value_usd = quote_amount * want_price
                    quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym} (~${float(quote_value_usd):,.2f})"
                    if usd_value > 0 and quote_value_usd > 0:
                        mismatch_ratio = abs(usd_value - quote_value_usd) / usd_value
                        if mismatch_ratio >= Decimal("0.20"):
                            divergence_line = (
                                f"⚠️  Warning: sell value and quote value differ by {float(mismatch_ratio * 100):,.1f}%"
                            )
                except (InvalidOperation, ValueError, TypeError):
                    quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym}"
            else:
                quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym}"

            rate_line = None
            if amount > 0:
                quote_rate = quote_amount / Decimal(str(amount))
                start_rate = Decimal(starting_price) / Decimal(str(amount))
                min_rate = Decimal(minimum_price) / Decimal(str(amount))
                rate_line = (
                    f"  Rate:        {float(quote_rate):,.4f} quoted | {float(start_rate):,.4f} start | "
                    f"{float(min_rate):,.4f} floor {want_sym}/{token_sym}"
                )
            precision_line = None
            if quote_amount > 0 and Decimal(starting_price) > quote_amount * 2:
                precision_line = f"               \u21b3 ceiled lot based on {quote_amount:.4f} quote"

            content = []
            if divergence_line:
                content.extend([divergence_line, ""])
            content.extend([
                "Kick (1 of 1)",
                f"  Source:      {source_name} ({short_address(k['source'])})",
                f"  Auction:     {k['auction']}",
                f"  Sell amount: {amount_str} {token_sym} (~${float(usd_value):,.2f})",
                quote_value_line,
                f"  Start quote: {k['starting_price_display']}",
                f"  Min price:   {k['minimum_price_display']}",
                f"  Profile:     {profile_name} | decay {step_decay_str}",
            ])
            if rate_line:
                content.append(rate_line)
            if precision_line:
                content.append(precision_line)
            content.extend([
                f"  Gas est:     {gas_estimate:,} (~{gas_cost_eth:.6f} ETH)",
                f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee} gwei",
            ])
        else:
            # Multi-kick batch display.
            content = [f"Batch of {batch_size} kicks", ""]
            for i, k in enumerate(kicks, 1):
                source_name = k.get("source_name") or "Unknown"
                token_sym = k.get("token_symbol") or "???"
                profile_name = k.get("pricing_profile_name") or "default"
                amount = float(k["sell_amount"])
                amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"
                usd_value = float(k["usd_value"])
                content.append(
                    f"  {i}. {source_name} | {amount_str} {token_sym} (~${usd_value:,.2f}) | {profile_name}"
                )

            total_usd = float(summary["total_usd"])
            content.extend([
                "",
                f"  Total USD:   ~${total_usd:,.2f}",
                f"  Gas est:     {gas_estimate:,} (~{gas_cost_eth:.6f} ETH)",
                f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee} gwei",
            ])

        width = max(len(line) for line in content)
        border = typer.style
        h_bar = "\u2500" * (width + 2)
        top = border(f"\u250c{h_bar}\u2510", fg="cyan")
        bottom = border(f"\u2514{h_bar}\u2518", fg="cyan")
        vl = border("\u2502", fg="cyan")

        lines = [top]
        for line in content:
            lines.append(f"{vl} {line.ljust(width)} {vl}")
        lines.append(bottom)

        candidate_label = "candidate" if batch_size == 1 else "candidates"
        typer.echo(f"{batch_size} {candidate_label} ready for submission")
        typer.echo()
        typer.echo("\n".join(lines))
        prompt = "Send this transaction?" if batch_size == 1 else f"Send batch of {batch_size} kicks?"
        accepted = typer.confirm(prompt, default=False)
        if accepted:
            typer.echo()
            typer.echo("Submitting transaction...")
        return accepted

    return _confirm_batch


def _run_txn_once(
    *,
    live: bool,
    confirm: bool,
    config: Path | None,
    batch: bool,
    verbose: bool = False,
    source_type: SourceType | None = None,
    output: OutputMode | None = None,
) -> None:
    """Execute a single transaction evaluation cycle."""

    if confirm:
        live = True

    output_mode = _resolve_txn_output_mode(requested=output, confirm=confirm)
    if confirm and output_mode is OutputMode.JSON:
        raise typer.BadParameter("interactive confirmation requires --output text", param_hint="--output")

    configure_logging(verbose=verbose, output_mode=output_mode)
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if live:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output_mode is OutputMode.TEXT:
        typer.echo(f"Evaluating {_txn_scope_label(source_type)}...")

    confirm_fn = _make_confirm_fn() if confirm else None

    skip_base_fee_check = False
    web3_client = build_web3_client(settings) if live else None
    if web3_client is not None:
        base_fee_wei = asyncio.run(web3_client.get_base_fee())
        base_fee_gwei = base_fee_wei / 1e9
        if base_fee_gwei > settings.txn_max_base_fee_gwei:
            typer.echo(
                f"Warning: base fee is {base_fee_gwei:.2f} gwei (limit: {settings.txn_max_base_fee_gwei} gwei)"
            )
            typer.confirm("Continue despite high gas?", default=False, abort=True)
            skip_base_fee_check = True

    db = Database(settings.database_url)
    with db.session() as session:
        txn_service = build_txn_service(
            settings, session,
            confirm_fn=confirm_fn,
            skip_base_fee_check=skip_base_fee_check,
            web3_client=web3_client,
        )
        result = asyncio.run(txn_service.run_once(live=live, batch=batch, source_type=source_type))
        if output_mode is OutputMode.TEXT:
            run_rows = _load_run_rows(session, result.run_id)
            _echo_txn_text_summary(
                result=result,
                live=live,
                source_type=source_type,
                run_rows=run_rows,
                verbose=verbose,
            )


@txn_app.callback()
def txn(
    ctx: typer.Context,
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    confirm: bool = typer.Option(default=False, help="Interactive confirmation before each kick (implies --live)"),
    batch: bool = typer.Option(default=False, help="Send a single batchKick() instead of individual kick() per candidate"),
    source_type: str | None = typer.Option(None, "--type", help="Filter candidates by source type: strategy or fee-burner"),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
    output: OutputMode | None = typer.Option(default=None, help="Output mode: text for operators, json for automation"),
    verbose: bool = typer.Option(default=False, help="Show per-candidate failure details and grouped summary"),
) -> None:
    """Evaluate kick candidates and send transactions."""
    if ctx.invoked_subcommand is not None:
        return
    normalized_source_type = _normalize_source_type_filter(source_type)
    _run_txn_once(
        live=live,
        confirm=confirm,
        config=config,
        batch=batch,
        verbose=verbose,
        source_type=normalized_source_type,
        output=output,
    )


@txn_app.command("daemon")
def txn_daemon(
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    batch: bool = typer.Option(default=True, help="Use batchKick (default) or individual kick() per candidate"),
    interval_seconds: int | None = typer.Option(default=None, min=1),
    source_type: str | None = typer.Option(None, "--type", help="Filter candidates by source type: strategy or fee-burner"),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
    output: OutputMode | None = typer.Option(default=None, help="Output mode: text for operators, json for automation"),
    verbose: bool = typer.Option(default=False, help="Show per-candidate failure details and grouped summary"),
) -> None:
    """Run the transaction service continuously."""

    output_mode = _resolve_txn_output_mode(requested=output, confirm=False, daemon=True)
    configure_logging(verbose=verbose, output_mode=output_mode)
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if live:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    normalized_source_type = _normalize_source_type_filter(source_type)
    sleep_seconds = interval_seconds or 1800

    async def _run() -> None:
        while True:
            web3_client = build_web3_client(settings)
            try:
                base_fee_wei = await web3_client.get_base_fee()
                base_fee_gwei = base_fee_wei / 1e9
            except Exception:  # noqa: BLE001
                base_fee_gwei = 0.0

            if base_fee_gwei > settings.txn_max_base_fee_gwei:
                logger.info(
                    "txn_daemon_skip_high_base_fee",
                    base_fee_gwei=f"{base_fee_gwei:.2f}",
                    limit_gwei=settings.txn_max_base_fee_gwei,
                )
                await asyncio.sleep(sleep_seconds)
                continue

            db = Database(settings.database_url)
            with db.session() as session:
                txn_service = build_txn_service(settings, session, web3_client=web3_client)
                result = await txn_service.run_once(live=live, batch=batch, source_type=normalized_source_type)
                if output_mode is OutputMode.TEXT:
                    run_rows = _load_run_rows(session, result.run_id)
                    _echo_txn_text_summary(
                        result=result,
                        live=live,
                        source_type=normalized_source_type,
                        run_rows=run_rows,
                        verbose=verbose,
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
