"""CLI entrypoint for factory-dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import typer

from factory_dashboard.config import load_settings
from factory_dashboard.errors import ConfigurationError
from factory_dashboard.health import run_healthcheck
from factory_dashboard.logging import configure_logging
from factory_dashboard.migrations import run_migrations
from factory_dashboard.normalizers import short_address
from factory_dashboard.persistence.db import Database
from factory_dashboard.runtime import build_scanner_service, build_txn_service, build_web3_client

app = typer.Typer(help="Factory dashboard scanner CLI")
db_app = typer.Typer(help="Database commands")
scan_app = typer.Typer(help="Scanner commands")
txn_app = typer.Typer(help="Transaction service commands")

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


@scan_app.command("once")
def scan_once(
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run a single scan cycle."""

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


def _make_confirm_fn() -> Callable[[dict], bool]:
    """Return a confirm callback with its own kick counter."""
    counter = 0

    def _confirm_kick(summary: dict) -> bool:
        nonlocal counter
        counter += 1

        strategy_name = summary.get("strategy_name") or "Unknown"
        token_sym = summary.get("token_symbol") or "???"
        want_sym = summary.get("want_symbol") or "???"

        amount = float(summary["sell_amount"])
        amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"

        # Implied want-token USD price from quote.
        quote_amount = float(summary["quote_amount"])
        usd_value = float(summary["usd_value"])
        want_price_str = f"~${usd_value / quote_amount:,.2f}/{want_sym}" if quote_amount else ""

        gas_cost_eth = summary.get("gas_cost_eth", 0)
        priority_fee = summary.get("priority_fee_gwei", 0)
        max_fee = summary.get("max_fee_gwei", 0)

        # Detect when ceiling inflated startingPrice significantly.
        starting_price = int(summary["starting_price"])
        precision_line = None
        if quote_amount > 0 and starting_price > quote_amount * 2:
            precision_line = (
                f"               \u21b3 ceiled lot based on {quote_amount:.4f} quote"
            )

        content = [
            f"Kick #{counter}",
            f"  Strategy:    {strategy_name} ({short_address(summary['strategy'])})",
            f"  Auction:     {summary['auction']}",
            f"  Sell amount: {amount_str} {token_sym} (~${usd_value:,.2f})",
            f"  Start quote: {summary['starting_price_display']} | {want_price_str}",
        ]
        if precision_line:
            content.append(precision_line)
        content.extend([
            f"  Gas est:     {summary['gas_estimate']:,} (~{gas_cost_eth:.6f} ETH)",
            f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee} gwei",
        ])

        width = max(len(line) for line in content)
        border = typer.style
        top = border(f"\u250c{'\u2500' * (width + 2)}\u2510", fg="cyan")
        bottom = border(f"\u2514{'\u2500' * (width + 2)}\u2518", fg="cyan")
        vl = border("\u2502", fg="cyan")

        lines = [top]
        for line in content:
            lines.append(f"{vl} {line.ljust(width)} {vl}")
        lines.append(bottom)

        typer.echo("\n".join(lines))
        return typer.confirm("Send this transaction?", default=False)

    return _confirm_kick


@txn_app.command("once")
def txn_once(
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    confirm: bool = typer.Option(default=False, help="Interactive confirmation before each kick (implies --live)"),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run a single transaction evaluation cycle."""

    if confirm:
        live = True

    configure_logging()
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if live:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    confirm_fn = _make_confirm_fn() if confirm else None

    db = Database(settings.database_url)
    with db.session() as session:
        txn_service = build_txn_service(settings, session, confirm_fn=confirm_fn)
        result = asyncio.run(txn_service.run_once(live=live))
        typer.echo(
            (
                f"txn_complete run_id={result.run_id} status={result.status} "
                f"candidates={result.candidates_found} attempted={result.kicks_attempted} "
                f"succeeded={result.kicks_succeeded} failed={result.kicks_failed}"
            )
        )


@txn_app.command("daemon")
def txn_daemon(
    live: bool = typer.Option(default=False, help="Send transactions (default: dry-run)"),
    interval_seconds: int | None = typer.Option(default=None, min=1),
    config: Path | None = typer.Option(default=None, exists=True, file_okay=True, dir_okay=False),
) -> None:
    """Run the transaction service continuously."""

    configure_logging()
    settings = load_settings(config)
    try:
        _require_rpc_url(settings)
        if live:
            _require_keystore(settings)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    sleep_seconds = interval_seconds or 1800

    async def _run() -> None:
        while True:
            db = Database(settings.database_url)
            with db.session() as session:
                txn_service = build_txn_service(settings, session)
                result = await txn_service.run_once(live=live)
                typer.echo(
                    (
                        f"txn_complete run_id={result.run_id} status={result.status} "
                        f"candidates={result.candidates_found} attempted={result.kicks_attempted} "
                        f"succeeded={result.kicks_succeeded} failed={result.kicks_failed}"
                    )
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
