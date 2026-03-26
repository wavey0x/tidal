"""CLI entrypoint for factory-dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import structlog
import typer

from factory_dashboard.config import load_settings
from factory_dashboard.errors import ConfigurationError
from factory_dashboard.health import run_healthcheck
from factory_dashboard.logging import configure_logging
from factory_dashboard.migrations import run_migrations
from factory_dashboard.normalizers import short_address
from factory_dashboard.persistence.db import Database
from factory_dashboard.runtime import build_scanner_service, build_txn_service, build_web3_client
from factory_dashboard.transaction_service.types import SourceType

logger = structlog.get_logger(__name__)

app = typer.Typer(help="Factory dashboard scanner CLI")
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
            amount = float(k["sell_amount"])
            amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"
            quote_amount = float(k["quote_amount"])
            usd_value = float(k["usd_value"])
            want_price_str = f"~${usd_value / quote_amount:,.2f}/{want_sym}" if quote_amount else ""

            starting_price = int(k["starting_price"])
            minimum_price = int(k["minimum_price"])
            min_usd_str = f"~${usd_value / minimum_price:,.2f}/{want_sym}" if minimum_price else ""
            precision_line = None
            if quote_amount > 0 and starting_price > quote_amount * 2:
                precision_line = f"               \u21b3 ceiled lot based on {quote_amount:.4f} quote"

            content = [
                "Kick (1 of 1)",
                f"  Source:      {source_name} ({short_address(k['source'])})",
                f"  Auction:     {k['auction']}",
                f"  Sell amount: {amount_str} {token_sym} (~${usd_value:,.2f})",
                f"  Start quote: {k['starting_price_display']} | {want_price_str}",
                f"  Min price:   {k['minimum_price_display']} | {min_usd_str}",
            ]
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
                amount = float(k["sell_amount"])
                amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"
                usd_value = float(k["usd_value"])
                content.append(
                    f"  {i}. {source_name} | {amount_str} {token_sym} (~${usd_value:,.2f})"
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

        typer.echo("\n".join(lines))
        prompt = "Send this transaction?" if batch_size == 1 else f"Send batch of {batch_size} kicks?"
        return typer.confirm(prompt, default=False)

    return _confirm_batch


def _run_txn_once(
    *,
    live: bool,
    confirm: bool,
    config: Path | None,
    batch: bool,
    verbose: bool = False,
    source_type: SourceType | None = None,
) -> None:
    """Execute a single transaction evaluation cycle."""

    if confirm:
        live = True

    configure_logging(verbose=verbose)
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if live:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

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
        type_suffix = f" type={_display_source_type_filter(source_type)}" if source_type is not None else ""
        typer.echo(
            (
                f"txn_complete run_id={result.run_id} status={result.status} "
                f"candidates={result.candidates_found} attempted={result.kicks_attempted} "
                f"succeeded={result.kicks_succeeded} failed={result.kicks_failed}"
                f"{type_suffix}"
            )
        )
        if verbose and result.failure_summary:
            typer.echo(
                f"txn_failure_summary run_id={result.run_id} "
                + " ".join(f"{k}={v}" for k, v in result.failure_summary.items())
            )


@txn_app.callback()
def txn(
    ctx: typer.Context,
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    confirm: bool = typer.Option(default=False, help="Interactive confirmation before each kick (implies --live)"),
    batch: bool = typer.Option(default=False, help="Send a single batchKick() instead of individual kick() per candidate"),
    source_type: str | None = typer.Option(None, "--type", help="Filter candidates by source type: strategy or fee-burner"),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
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
    )


@txn_app.command("daemon")
def txn_daemon(
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    batch: bool = typer.Option(default=True, help="Use batchKick (default) or individual kick() per candidate"),
    interval_seconds: int | None = typer.Option(default=None, min=1),
    source_type: str | None = typer.Option(None, "--type", help="Filter candidates by source type: strategy or fee-burner"),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
    verbose: bool = typer.Option(default=False, help="Show per-candidate failure details and grouped summary"),
) -> None:
    """Run the transaction service continuously."""

    configure_logging(verbose=verbose)
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
                type_suffix = (
                    f" type={_display_source_type_filter(normalized_source_type)}"
                    if normalized_source_type is not None
                    else ""
                )
                typer.echo(
                    (
                        f"txn_complete run_id={result.run_id} status={result.status} "
                        f"candidates={result.candidates_found} attempted={result.kicks_attempted} "
                        f"succeeded={result.kicks_succeeded} failed={result.kicks_failed}"
                        f"{type_suffix}"
                    )
                )
                if verbose and result.failure_summary:
                    typer.echo(
                        f"txn_failure_summary run_id={result.run_id} "
                        + " ".join(f"{k}={v}" for k, v in result.failure_summary.items())
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
