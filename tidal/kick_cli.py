"""Kick command group."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation

import typer
from sqlalchemy import select

from tidal.cli_context import CLIContext
from tidal.cli_exit_codes import kick_exit_code
from tidal.cli_options import (
    AccountOption,
    AuctionAddressOption,
    BroadcastOption,
    BypassConfirmationOption,
    ConfigOption,
    IntervalOption,
    JsonOption,
    KeystoreOption,
    LimitOption,
    PasswordFileOption,
    SenderOption,
    SourceAddressOption,
    SourceTypeOption,
    VerboseOption,
)
from tidal.cli_renderers import emit_json, kick_scope_label, render_kick_inspect, render_kick_run_summary
from tidal.errors import AddressNormalizationError, ConfigurationError
from tidal.logging import OutputMode, configure_logging
from tidal.normalizers import normalize_address, short_address
from tidal.ops.kick_inspect import inspect_kick_candidates
from tidal.persistence import models
from tidal.runtime import build_txn_service
from tidal.transaction_service.types import SourceType

app = typer.Typer(help="Kick auction lots", no_args_is_help=True)


def _to_dict(value: object) -> dict[str, object]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError("unsupported result payload")


def _normalize_source_type_filter(value: str | None) -> SourceType | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"strategy", "fee_burner"}:
        return normalized  # type: ignore[return-value]
    raise typer.BadParameter("expected 'strategy' or 'fee-burner'", param_hint="--source-type")


def _normalize_address_filter(value: str | None, *, param_hint: str) -> str | None:
    if value is None:
        return None
    try:
        return normalize_address(value.strip())
    except AddressNormalizationError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc


def _load_run_rows(session, run_id: str) -> list[dict[str, object]]:
    stmt = select(models.kick_txs).where(models.kick_txs.c.run_id == run_id).order_by(models.kick_txs.c.id.asc())
    return [dict(row) for row in session.execute(stmt).mappings().all()]


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
            step_decay_str = f"{step_decay_rate_bps / 100:.2f}%" if step_decay_rate_bps is not None else "—"
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
                precision_line = f"               ↳ ceiled lot based on {quote_amount:.4f} quote"

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
            content = [f"Batch of {batch_size} kicks", ""]
            for index, kick in enumerate(kicks, 1):
                source_name = kick.get("source_name") or "Unknown"
                token_sym = kick.get("token_symbol") or "???"
                profile_name = kick.get("pricing_profile_name") or "default"
                amount = float(kick["sell_amount"])
                amount_str = f"{amount:,.4f}" if amount < 1 else f"{amount:,.2f}"
                usd_value = float(kick["usd_value"])
                content.append(f"  {index}. {source_name} | {amount_str} {token_sym} (~${usd_value:,.2f}) | {profile_name}")

            total_usd = float(summary["total_usd"])
            content.extend([
                "",
                f"  Total USD:   ~${total_usd:,.2f}",
                f"  Gas est:     {gas_estimate:,} (~{gas_cost_eth:.6f} ETH)",
                f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee} gwei",
            ])

        width = max(len(line) for line in content)
        h_bar = "─" * (width + 2)
        top = typer.style(f"┌{h_bar}┐", fg="cyan")
        bottom = typer.style(f"└{h_bar}┘", fg="cyan")
        vl = typer.style("│", fg="cyan")
        lines = [top, *(f"{vl} {line.ljust(width)} {vl}" for line in content), bottom]
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


@app.command("run")
def kick_run(
    config: ConfigOption = None,
    broadcast: BroadcastOption = False,
    bypass_confirmation: BypassConfirmationOption = False,
    json_output: JsonOption = False,
    source_type: SourceTypeOption = None,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = None,
    sender: SenderOption = None,
    account: AccountOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    verbose: VerboseOption = False,
    explain: bool = typer.Option(False, "--explain", help="Show explainability details for the selected candidates."),
    require_curve_quote: bool | None = typer.Option(
        None,
        "--require-curve-quote/--allow-missing-curve-quote",
        help="Override Curve quote strictness for this run.",
    ),
) -> None:
    """Evaluate kick candidates and optionally send transactions."""

    configure_logging(verbose=verbose, output_mode=OutputMode.TEXT)
    if bypass_confirmation and not broadcast:
        raise typer.BadParameter("--bypass-confirmation requires --broadcast", param_hint="--bypass-confirmation")
    cli_ctx = CLIContext(config)
    try:
        cli_ctx.require_rpc()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    normalized_source_type = _normalize_source_type_filter(source_type)
    normalized_source_address = _normalize_address_filter(source_address, param_hint="--source")
    normalized_auction_address = _normalize_address_filter(auction_address, param_hint="--auction")
    normalized_sender = _normalize_address_filter(sender, param_hint="--sender")
    signer = cli_ctx.resolve_signer(
        required=broadcast,
        required_for="broadcast kick execution",
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    cli_ctx.validate_sender(
        sender=normalized_sender,
        signer=signer,
        required_for="broadcast kick execution",
    )
    execution_sender = None
    if broadcast:
        execution_sender = cli_ctx.resolve_sender(
            sender=normalized_sender,
            account_name=account,
            keystore_path=keystore,
            signer=signer,
        )

    inspection_data = None
    if explain:
        with cli_ctx.session() as session:
            inspection_data = inspect_kick_candidates(
                session,
                cli_ctx.settings,
                source_type=normalized_source_type,
                source_address=normalized_source_address,
                auction_address=normalized_auction_address,
                limit=limit,
            )

    confirm_fn = None if bypass_confirmation or not broadcast else _make_confirm_fn()
    skip_base_fee_check = False
    web3_client = cli_ctx.web3_client() if broadcast else None
    if web3_client is not None:
        base_fee_wei = asyncio.run(web3_client.get_base_fee())
        base_fee_gwei = base_fee_wei / 1e9
        if base_fee_gwei > cli_ctx.settings.txn_max_base_fee_gwei:
            typer.echo(f"Warning: base fee is {base_fee_gwei:.2f} gwei (limit: {cli_ctx.settings.txn_max_base_fee_gwei} gwei)")
            if bypass_confirmation:
                skip_base_fee_check = True
            else:
                typer.confirm("Continue despite high gas?", default=False, abort=True)
                skip_base_fee_check = True

    if not json_output:
        typer.echo(f"Evaluating {kick_scope_label(normalized_source_type, source_address=normalized_source_address, auction_address=normalized_auction_address)}...")

    with cli_ctx.session() as session:
        txn_service = build_txn_service(
            cli_ctx.settings,
            session,
            confirm_fn=confirm_fn,
            require_curve_quote=require_curve_quote,
            skip_base_fee_check=skip_base_fee_check,
            web3_client=web3_client,
            signer=signer,
        )
        result = asyncio.run(
            txn_service.run_once(
                live=broadcast,
                batch=False,
                source_type=normalized_source_type,
                source_address=normalized_source_address,
                auction_address=normalized_auction_address,
                limit=limit,
            )
        )
        run_rows = _load_run_rows(session, result.run_id)

    status = "ok"
    if result.candidates_found == 0:
        status = "noop"
    elif broadcast and result.kicks_failed and result.kicks_succeeded:
        status = "error"
    elif broadcast and result.kicks_failed:
        status = "error"

    if json_output:
        data = {
            "run": _to_dict(result),
            "rows": run_rows,
            "sender": execution_sender,
        }
        if inspection_data is not None:
            data["inspection"] = asdict(inspection_data)
        emit_json("kick.run", status=status, data=data)
    else:
        render_kick_run_summary(
            result=result,
            live=broadcast,
            source_type=normalized_source_type,
            source_address=normalized_source_address,
            auction_address=normalized_auction_address,
            run_rows=run_rows,
            verbose=verbose,
            sender=execution_sender,
        )
        if inspection_data is not None:
            typer.echo()
            render_kick_inspect(inspection_data, show_all=True)

    raise typer.Exit(code=kick_exit_code(
        live=broadcast,
        status=result.status,
        candidates_found=result.candidates_found,
        kicks_failed=result.kicks_failed,
    ))


@app.command("daemon")
def kick_daemon(
    config: ConfigOption = None,
    broadcast: BroadcastOption = False,
    json_output: JsonOption = False,
    source_type: SourceTypeOption = None,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = None,
    interval_seconds: IntervalOption = None,
    sender: SenderOption = None,
    account: AccountOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    verbose: VerboseOption = False,
    require_curve_quote: bool | None = typer.Option(
        None,
        "--require-curve-quote/--allow-missing-curve-quote",
        help="Override Curve quote strictness for this run.",
    ),
) -> None:
    """Run the kick service continuously."""

    configure_logging(verbose=verbose, output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config)
    try:
        cli_ctx.require_rpc()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    normalized_source_type = _normalize_source_type_filter(source_type)
    normalized_source_address = _normalize_address_filter(source_address, param_hint="--source")
    normalized_auction_address = _normalize_address_filter(auction_address, param_hint="--auction")
    normalized_sender = _normalize_address_filter(sender, param_hint="--sender")
    signer = cli_ctx.resolve_signer(
        required=broadcast,
        required_for="broadcast kick daemon execution",
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    cli_ctx.validate_sender(
        sender=normalized_sender,
        signer=signer,
        required_for="broadcast kick daemon execution",
    )
    execution_sender = None
    if broadcast:
        execution_sender = cli_ctx.resolve_sender(
            sender=normalized_sender,
            account_name=account,
            keystore_path=keystore,
            signer=signer,
        )
    sleep_seconds = interval_seconds or 1800

    async def run_loop() -> None:
        while True:
            web3_client = cli_ctx.web3_client()
            try:
                base_fee_wei = await web3_client.get_base_fee()
                base_fee_gwei = base_fee_wei / 1e9
            except Exception:
                base_fee_gwei = 0.0

            if broadcast and base_fee_gwei > cli_ctx.settings.txn_max_base_fee_gwei:
                await asyncio.sleep(sleep_seconds)
                continue

            with cli_ctx.session() as session:
                txn_service = build_txn_service(
                    cli_ctx.settings,
                    session,
                    require_curve_quote=require_curve_quote,
                    web3_client=web3_client,
                    signer=signer,
                    skip_base_fee_check=broadcast and base_fee_gwei > cli_ctx.settings.txn_max_base_fee_gwei,
                )
                result = await txn_service.run_once(
                    live=broadcast,
                    batch=True,
                    source_type=normalized_source_type,
                    source_address=normalized_source_address,
                    auction_address=normalized_auction_address,
                    limit=limit,
                )
                run_rows = _load_run_rows(session, result.run_id)
            if json_output:
                emit_json(
                    "kick.daemon",
                    status="ok" if result.candidates_found else "noop",
                    data={"run": asdict(result), "rows": run_rows, "sender": execution_sender},
                )
            else:
                render_kick_run_summary(
                    result=result,
                    live=broadcast,
                    source_type=normalized_source_type,
                    source_address=normalized_source_address,
                    auction_address=normalized_auction_address,
                    run_rows=run_rows,
                    verbose=verbose,
                    sender=execution_sender,
                )
            await asyncio.sleep(sleep_seconds)

    asyncio.run(run_loop())


@app.command("inspect")
def kick_inspect(
    config: ConfigOption = None,
    json_output: JsonOption = False,
    source_type: SourceTypeOption = None,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = None,
    show_all: bool = typer.Option(False, "--show-all", help="Show deferred and limited candidates in addition to ready/cooldown."),
) -> None:
    """Explain why candidates are ready, skipped, or deferred."""

    configure_logging(output_mode=OutputMode.TEXT)
    cli_ctx = CLIContext(config)
    normalized_source_type = _normalize_source_type_filter(source_type)
    normalized_source_address = _normalize_address_filter(source_address, param_hint="--source")
    normalized_auction_address = _normalize_address_filter(auction_address, param_hint="--auction")

    with cli_ctx.session() as session:
        result = inspect_kick_candidates(
            session,
            cli_ctx.settings,
            source_type=normalized_source_type,
            source_address=normalized_source_address,
            auction_address=normalized_auction_address,
            limit=limit,
        )

    status = "ok" if (result.ready_count or result.cooldown_count or result.deferred_same_auction_count or result.limited_count) else "noop"
    if json_output:
        emit_json("kick.inspect", status=status, data=asdict(result))
    else:
        render_kick_inspect(result, show_all=show_all)
    raise typer.Exit(code=0 if status == "ok" else 2)
