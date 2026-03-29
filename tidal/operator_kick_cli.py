"""API-backed kick commands."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import typer

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import (
    AccountOption,
    ApiBaseUrlOption,
    ApiKeyOption,
    AuctionAddressOption,
    BroadcastOption,
    BypassConfirmationOption,
    ConfigOption,
    JsonOption,
    KeystoreOption,
    LimitOption,
    PasswordFileOption,
    SenderOption,
    SourceAddressOption,
    SourceTypeOption,
    VerboseOption,
)
from tidal.cli_renderers import emit_json, render_confirmation_banner, render_kick_inspect, render_kick_submission_summary
from tidal.control_plane.client import ControlPlaneError
from tidal.operator_cli_support import (
    execute_prepared_action_sync,
    render_action_preview,
    render_broadcast_result,
    render_submission_outcome,
    submission_progress,
    render_warnings,
)
from tidal.ops.kick_inspect import KickInspectEntry, KickInspectResult

app = typer.Typer(help="Kick auction lots", no_args_is_help=True)


def _normalize_source_type_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"strategy", "fee_burner"}:
        return normalized
    raise typer.BadParameter("expected 'strategy' or 'fee-burner'", param_hint="--source-type")


def _inspect_result_from_api(data: dict[str, object]) -> KickInspectResult:
    return KickInspectResult(
        source_type=data["source_type"],
        source_address=data["source_address"],
        auction_address=data["auction_address"],
        limit=data["limit"],
        eligible_count=data["eligible_count"],
        selected_count=data["selected_count"],
        ready_count=data["ready_count"],
        cooldown_count=data["cooldown_count"],
        deferred_same_auction_count=data["deferred_same_auction_count"],
        limited_count=data["limited_count"],
        ready=[KickInspectEntry(**entry) for entry in data["ready"]],
        cooldown_skips=[KickInspectEntry(**entry) for entry in data["cooldown_skips"]],
        deferred_same_auction=[KickInspectEntry(**entry) for entry in data["deferred_same_auction"]],
        limited=[KickInspectEntry(**entry) for entry in data["limited"]],
    )


def _candidate_prepare_payload(candidate: KickInspectEntry, *, sender: str | None) -> dict[str, object]:
    return {
        "sourceType": candidate.source_type,
        "sourceAddress": candidate.source_address,
        "auctionAddress": candidate.auction_address,
        "tokenAddress": candidate.token_address,
        "limit": 1,
        "sender": sender,
    }


def _prepare_skip_messages(data: dict[str, object]) -> list[str]:
    preview = data.get("preview")
    if not isinstance(preview, dict):
        return []

    skipped = preview.get("skippedDuringPrepare")
    if not isinstance(skipped, list):
        return []

    messages: list[str] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        token_label = str(entry.get("tokenSymbol") or entry.get("tokenAddress") or "candidate")
        reason = str(entry.get("reason") or "candidate was skipped during prepare")
        messages.append(f"{token_label}: {reason}")
    return messages


def _resolve_preview_fee_context(cli_ctx: CLIContext) -> dict[str, float]:
    base_fee_gwei = 0.0
    max_priority = float(cli_ctx.settings.txn_max_priority_fee_gwei)
    priority_fee_gwei = max_priority

    try:
        web3_client = cli_ctx.web3_client()
    except Exception:
        web3_client = None

    if web3_client is not None:
        async def _fetch_fees() -> tuple[object, object]:
            try:
                return await asyncio.gather(
                    web3_client.get_base_fee(),
                    web3_client.get_max_priority_fee(),
                    return_exceptions=True,
                )
            finally:
                await web3_client.close()

        try:
            base_result, priority_result = asyncio.run(_fetch_fees())
            if not isinstance(base_result, BaseException):
                base_fee_gwei = base_result / 1e9
            if not isinstance(priority_result, BaseException):
                priority_fee_gwei = min(priority_result / 1e9, max_priority)
        except Exception:
            pass

    return {
        "base_fee_gwei": base_fee_gwei,
        "priority_fee_gwei": priority_fee_gwei,
        "max_fee_per_gas_gwei": max(float(cli_ctx.settings.txn_max_base_fee_gwei), base_fee_gwei) + max_priority,
    }


def _kick_submission_summary(
    data: dict[str, Any],
    *,
    candidate: KickInspectEntry,
    single_title: str,
    fee_context: dict[str, float],
    default_buffer_bps: int,
    default_min_buffer_bps: int,
    quote_spot_warning_threshold_pct: float,
) -> dict[str, object] | None:
    preview = data.get("preview")
    transactions = data.get("transactions")
    if not isinstance(preview, dict) or not isinstance(transactions, list) or not transactions:
        return None

    prepared_operations = preview.get("preparedOperations")
    if not isinstance(prepared_operations, list) or len(prepared_operations) != 1:
        return None

    prepared = prepared_operations[0]
    if not isinstance(prepared, dict) or prepared.get("operation") != "kick":
        return None

    transaction = transactions[0]
    if not isinstance(transaction, dict):
        return None

    gas_estimate = int(transaction["gasEstimate"]) if transaction.get("gasEstimate") is not None else None
    gas_limit = int(transaction["gasLimit"]) if transaction.get("gasLimit") is not None else None
    base_fee_gwei = fee_context["base_fee_gwei"]
    source_address = prepared.get("sourceAddress") or candidate.source_address
    source_name = prepared.get("sourceName") or candidate.source_name
    token_address = prepared.get("tokenAddress") or candidate.token_address
    token_symbol = prepared.get("tokenSymbol") or candidate.token_symbol
    auction_address = prepared.get("auctionAddress") or candidate.auction_address
    want_symbol = prepared.get("wantSymbol") or candidate.want_symbol
    starting_price = prepared.get("startingPrice")
    minimum_price = prepared.get("minimumPrice")
    buffer_bps = int(prepared.get("bufferBps") or default_buffer_bps)
    min_buffer_bps = int(prepared.get("minBufferBps") or default_min_buffer_bps)

    starting_price_display = prepared.get("startingPriceDisplay")
    if starting_price_display is None and starting_price is not None:
        starting_price_display = (
            f"{int(str(starting_price)):,} {want_symbol or '???'} (+{buffer_bps / 100:.0f}% buffer)"
        )

    minimum_price_display = prepared.get("minimumPriceDisplay")
    if minimum_price_display is None and minimum_price is not None:
        minimum_price_display = (
            f"{int(str(minimum_price)):,} {want_symbol or '???'} (-{min_buffer_bps / 100:.0f}% buffer)"
        )

    return {
        "single_title": single_title,
        "kicks": [
            {
                "source": source_address,
                "source_name": source_name,
                "source_type": prepared.get("sourceType"),
                "sender": transaction.get("sender"),
                "strategy": source_address,
                "strategy_name": source_name,
                "token": token_address,
                "token_symbol": token_symbol,
                "auction": auction_address,
                "sell_amount": prepared.get("sellAmount"),
                "usd_value": prepared.get("usdValue"),
                "starting_price": starting_price,
                "starting_price_display": starting_price_display,
                "minimum_price": minimum_price,
                "minimum_price_display": minimum_price_display,
                "want_address": prepared.get("wantAddress"),
                "want_symbol": want_symbol,
                "want_price_usd": prepared.get("wantPriceUsd"),
                "buffer_bps": buffer_bps,
                "min_buffer_bps": min_buffer_bps,
                "step_decay_rate_bps": prepared.get("stepDecayRateBps"),
                "pricing_profile_name": prepared.get("pricingProfileName"),
                "quote_amount": prepared.get("quoteAmount"),
                "settle_token": prepared.get("settleToken"),
            }
        ],
        "batch_size": 1,
        "total_usd": prepared.get("usdValue") or "0",
        "gas_estimate": gas_estimate,
        "gas_limit": gas_limit,
        "base_fee_gwei": base_fee_gwei,
        "priority_fee_gwei": fee_context["priority_fee_gwei"],
        "max_fee_per_gas_gwei": fee_context["max_fee_per_gas_gwei"],
        "gas_cost_eth": (gas_estimate * base_fee_gwei / 1e9) if gas_estimate is not None else None,
        "quote_spot_warning_threshold_pct": quote_spot_warning_threshold_pct,
    }


@app.command("inspect")
def kick_inspect(
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    json_output: JsonOption = False,
    source_type: SourceTypeOption = None,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = None,
    show_all: bool = typer.Option(False, "--show-all", help="Show deferred and limited candidates."),
) -> None:
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    payload = {
        "sourceType": _normalize_source_type_filter(source_type),
        "sourceAddress": normalize_cli_address(source_address, param_hint="--source"),
        "auctionAddress": normalize_cli_address(auction_address, param_hint="--auction"),
        "limit": limit,
    }
    try:
        with cli_ctx.control_plane_client(auth=False) as client:
            response = client.inspect_kicks(payload)
    except ControlPlaneError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    data = response["data"]
    if json_output:
        emit_json("kick.inspect", status=response["status"], data=data, warnings=response.get("warnings"))
    else:
        render_kick_inspect(_inspect_result_from_api(data), show_all=show_all)
    raise typer.Exit(code=0 if response["status"] == "ok" else 2)


@app.command("run")
def kick_run(
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
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
) -> None:
    del verbose
    if bypass_confirmation and not broadcast:
        raise typer.BadParameter("--bypass-confirmation requires --broadcast", param_hint="--bypass-confirmation")
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    normalized_source_type = _normalize_source_type_filter(source_type)
    normalized_source_address = normalize_cli_address(source_address, param_hint="--source")
    normalized_auction_address = normalize_cli_address(auction_address, param_hint="--auction")
    exec_ctx = cli_ctx.resolve_execution(
        broadcast=broadcast,
        required_for="broadcast kick execution",
        sender=normalize_cli_address(sender, param_hint="--sender"),
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    payload = {
        "sourceType": normalized_source_type,
        "sourceAddress": normalized_source_address,
        "auctionAddress": normalized_auction_address,
        "limit": limit,
        "sender": exec_ctx.sender,
        "includeLiveInspection": False,
    }
    preview_fee_context = _resolve_preview_fee_context(cli_ctx) if broadcast and not json_output else None
    try:
        with cli_ctx.control_plane_client(auth=broadcast) as client:
            inspect_response = client.inspect_kicks(payload)
            inspect_result = _inspect_result_from_api(inspect_response["data"])
            broadcast_records: list[dict[str, object]] = []
            prepared_actions: list[dict[str, object]] = []

            if broadcast and inspect_result.ready_count:
                total_ready = len(inspect_result.ready)
                for index, candidate in enumerate(inspect_result.ready, start=1):
                    prepare_response = client.prepare_kicks(
                        _candidate_prepare_payload(candidate, sender=exec_ctx.sender)
                    )
                    prepared_data = prepare_response["data"]
                    prepared_actions.append(prepared_data)
                    warnings = list(prepare_response.get("warnings") or [])
                    transactions = list(prepared_data.get("transactions") or [])

                    if prepare_response["status"] == "noop" or not transactions:
                        if not json_output:
                            for message in _prepare_skip_messages(prepared_data):
                                typer.echo(f"Skipped during prepare: {message}")
                            render_warnings(warnings)
                        continue

                    if not json_output:
                        summary = _kick_submission_summary(
                            prepared_data,
                            candidate=candidate,
                            single_title=f"Kick ({index} of {total_ready})",
                            fee_context=preview_fee_context or {
                                "base_fee_gwei": 0.0,
                                "priority_fee_gwei": float(cli_ctx.settings.txn_max_priority_fee_gwei),
                                "max_fee_per_gas_gwei": (
                                    max(float(cli_ctx.settings.txn_max_base_fee_gwei), 0.0)
                                    + float(cli_ctx.settings.txn_max_priority_fee_gwei)
                                ),
                            },
                            default_buffer_bps=cli_ctx.settings.txn_start_price_buffer_bps,
                            default_min_buffer_bps=cli_ctx.settings.txn_min_price_buffer_bps,
                            quote_spot_warning_threshold_pct=(
                                cli_ctx.settings.txn_quote_spot_warning_threshold_pct
                            ),
                        )
                        if summary is not None:
                            render_kick_submission_summary(summary)
                        else:
                            render_action_preview(
                                prepared_data,
                                heading=f"Prepared kick action ({index}/{total_ready})",
                            )
                        render_warnings(warnings)
                    if not bypass_confirmation:
                        render_confirmation_banner("Send this transaction?")
                    if not bypass_confirmation and not typer.confirm("Send this transaction?", default=False):
                        continue
                    if exec_ctx.signer is None or exec_ctx.sender is None:
                        raise typer.Exit(code=1)
                    if not json_output:
                        typer.echo()
                    with submission_progress("Submitting transaction..."):
                        action_records = execute_prepared_action_sync(
                            settings=cli_ctx.settings,
                            client=client,
                            action_id=str(prepared_data["actionId"]),
                            sender=exec_ctx.sender,
                            signer=exec_ctx.signer,
                            transactions=transactions,
                        )
                    if not json_output:
                        render_submission_outcome(action_records, chain_id=cli_ctx.settings.chain_id)
                    broadcast_records.extend(action_records)
    except ControlPlaneError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    status = "ok" if inspect_result.ready_count else "noop"
    if broadcast and not broadcast_records:
        status = "noop"

    if json_output:
        output: dict[str, object] = {
            "inspection": asdict(inspect_result),
        }
        if broadcast:
            output["preparedActions"] = prepared_actions
            output["broadcastRecords"] = broadcast_records
        emit_json("kick.run", status=status, data=output)
    else:
        if not broadcast:
            render_kick_inspect(inspect_result, show_all=True)
            if inspect_result.ready_count:
                typer.echo()
                typer.echo(
                    "Dry run only. Candidates are ranked from cached prices; quotes are prepared just-in-time during broadcast."
                )
            else:
                typer.echo("No ready kick candidates.")
        else:
            if broadcast_records:
                render_broadcast_result(broadcast_records)
            else:
                typer.echo("No kick transactions were sent.")

    if status == "noop":
        raise typer.Exit(code=2)
