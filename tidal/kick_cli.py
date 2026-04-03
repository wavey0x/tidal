"""API-backed kick commands."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

import typer
from eth_utils import to_checksum_address

from tidal.auction_price_units import format_buffer_pct, scaled_price_to_rate
from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import (
    ApiBaseUrlOption,
    ApiKeyOption,
    AuctionAddressOption,
    ConfigOption,
    JsonOption,
    KeystoreOption,
    LimitOption,
    NoConfirmationOption,
    PasswordFileOption,
    SourceAddressOption,
    SourceTypeOption,
    VerboseOption,
)
from tidal.cli_validation import require_no_confirmation_for_json
from tidal.cli_renderers import emit_json, render_kick_inspect, render_kick_submission_summary, render_skip_panel
from tidal.control_plane.client import ControlPlaneError
from tidal.errors import ConfigurationError
from tidal.operator_cli_support import (
    execute_prepared_action_sync,
    progress_status,
    render_action_preview,
    render_broadcast_result,
    render_warnings,
    submission_progress,
)
from tidal.ops.kick_inspect import KickInspectEntry, KickInspectResult

app = typer.Typer(help="Kick auction lots", no_args_is_help=True)


def _current_monotonic() -> float:
    return time.monotonic()


def _format_prepared_action_age_limit(max_age_seconds: int) -> str:
    if max_age_seconds % 60 == 0 and max_age_seconds >= 60:
        minutes = max_age_seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    unit = "second" if max_age_seconds == 1 else "seconds"
    return f"{max_age_seconds} {unit}"


def _prepared_action_is_stale(*, prepared_at_monotonic: float, max_age_seconds: int) -> bool:
    return _current_monotonic() - prepared_at_monotonic > max_age_seconds


def _prepared_action_stale_warning(max_age_seconds: int) -> str:
    return (
        "Prepared transaction expired after "
        f"{_format_prepared_action_age_limit(max_age_seconds)}; "
        "re-run to refresh quotes before sending."
    )


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
        ignored_count=data["ignored_count"],
        cooldown_count=data["cooldown_count"],
        deferred_same_auction_count=data["deferred_same_auction_count"],
        limited_count=data["limited_count"],
        ready=[KickInspectEntry(**entry) for entry in data["ready"]],
        ignored_skips=[KickInspectEntry(**entry) for entry in data["ignored_skips"]],
        cooldown_skips=[KickInspectEntry(**entry) for entry in data["cooldown_skips"]],
        deferred_same_auction=[KickInspectEntry(**entry) for entry in data["deferred_same_auction"]],
        limited=[KickInspectEntry(**entry) for entry in data["limited"]],
    )


def _candidate_prepare_payload(candidate: KickInspectEntry, *, sender: str | None) -> dict[str, object]:
    payload = {
        "sourceType": candidate.source_type,
        "sourceAddress": candidate.source_address,
        "auctionAddress": candidate.auction_address,
        "tokenAddress": candidate.token_address,
        "limit": 1,
        "sender": sender,
    }
    return payload


def _candidate_review_queue(
    inspect_result: KickInspectResult,
    *,
    include_deferred_same_auction: bool,
) -> list[KickInspectEntry]:
    queue = list(inspect_result.ready)
    if include_deferred_same_auction:
        queue.extend(inspect_result.deferred_same_auction)
    return queue


def _is_active_above_minimum_price_skip(reason: str | None) -> bool:
    if not reason:
        return False
    return "active above minimumprice" in reason.lower()


def _should_stop_after_same_auction_skip(
    *,
    candidate: KickInspectEntry,
    skip_entries: list[dict[str, str | None]],
    remaining_candidates: list[KickInspectEntry],
) -> bool:
    if not skip_entries or not remaining_candidates:
        return False

    candidate_auction = candidate.auction_address.lower()
    if not all(
        (entry.get("auction_address") or "").lower() == candidate_auction
        and _is_active_above_minimum_price_skip(entry.get("reason"))
        for entry in skip_entries
    ):
        return False

    return any(
        next_candidate.auction_address.lower() == candidate_auction
        for next_candidate in remaining_candidates
    )


def _prepare_skips(data: dict[str, object], *, candidate: KickInspectEntry | None = None) -> list[dict[str, str | None]]:
    preview = data.get("preview")
    if not isinstance(preview, dict):
        return []

    skipped = preview.get("skippedDuringPrepare")
    if not isinstance(skipped, list):
        return []

    skips: list[dict[str, str | None]] = []
    for entry in skipped:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "candidate was skipped during prepare")
        if reason:
            reason = reason[0].upper() + reason[1:]
        source_address = entry.get("sourceAddress")
        source_label = None
        if source_address:
            try:
                source_label = to_checksum_address(str(source_address))
            except Exception:
                source_label = str(source_address)
        auction_address = entry.get("auctionAddress")
        auction_label = None
        if auction_address:
            try:
                auction_label = to_checksum_address(str(auction_address))
            except Exception:
                auction_label = str(auction_address)
        skips.append(
            {
                "reason": reason,
                "token_symbol": (
                    str(entry.get("tokenSymbol"))
                    if entry.get("tokenSymbol") is not None
                    else candidate.token_symbol if candidate is not None else None
                ),
                "want_symbol": (
                    str(entry.get("wantSymbol"))
                    if entry.get("wantSymbol") is not None
                    else candidate.want_symbol if candidate is not None else None
                ),
                "source_name": (
                    str(entry.get("sourceName"))
                    if entry.get("sourceName") is not None
                    else candidate.source_name if candidate is not None else None
                ),
                "source_address": source_label or (candidate.source_address if candidate is not None else None),
                "auction_address": auction_label,
            }
        )
    return skips


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
    minimum_price_scaled_1e18 = prepared.get("minimumPriceScaled1e18") or prepared.get("minimumPrice")
    minimum_price = minimum_price_scaled_1e18
    minimum_quote = prepared.get("minimumQuote")
    buffer_bps = int(prepared.get("bufferBps") or default_buffer_bps)
    min_buffer_bps = int(prepared.get("minBufferBps") or default_min_buffer_bps)

    starting_price_display = prepared.get("startingPriceDisplay")
    if starting_price_display is None and starting_price is not None:
        starting_price_display = (
            f"{int(str(starting_price)):,} {want_symbol or '???'} (+{format_buffer_pct(buffer_bps)} buffer)"
        )

    minimum_quote_display = prepared.get("minimumQuoteDisplay")
    if minimum_quote_display is None and minimum_quote is not None:
        minimum_quote_display = (
            f"{int(str(minimum_quote)):,} {want_symbol or '???'} (-{format_buffer_pct(min_buffer_bps)} buffer)"
        )
    minimum_price_display = prepared.get("minimumPriceDisplay")
    if minimum_price_display is None and minimum_price_scaled_1e18 is not None:
        minimum_price_display = f"{int(str(minimum_price_scaled_1e18)):,} (scaled 1e18 floor)"

    floor_rate = prepared.get("floorRate")
    if floor_rate is None and minimum_price_scaled_1e18 is not None:
        scaled_floor_rate = scaled_price_to_rate(int(str(minimum_price_scaled_1e18)))
        floor_rate = str(scaled_floor_rate) if scaled_floor_rate is not None else None

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
                "minimum_price_scaled_1e18": minimum_price_scaled_1e18,
                "minimum_quote": minimum_quote,
                "minimum_quote_display": minimum_quote_display,
                "minimum_price_display": minimum_price_display,
                "want_address": prepared.get("wantAddress"),
                "want_symbol": want_symbol,
                "want_price_usd": prepared.get("wantPriceUsd"),
                "buffer_bps": buffer_bps,
                "min_buffer_bps": min_buffer_bps,
                "step_decay_rate_bps": prepared.get("stepDecayRateBps"),
                "pricing_profile_name": prepared.get("pricingProfileName"),
                "quote_amount": prepared.get("quoteAmount"),
                "quote_rate": prepared.get("quoteRate"),
                "start_rate": prepared.get("startRate"),
                "floor_rate": floor_rate,
                "settle_token": prepared.get("settleToken"),
                "recovery_plan": prepared.get("recoveryPlan"),
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
            if json_output:
                response = client.inspect_kicks(payload)
            else:
                with progress_status("Loading kick candidates..."):
                    response = client.inspect_kicks(payload)
    except (ConfigurationError, ControlPlaneError) as exc:
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
    no_confirmation: NoConfirmationOption = False,
    json_output: JsonOption = False,
    source_type: SourceTypeOption = None,
    source_address: SourceAddressOption = None,
    auction_address: AuctionAddressOption = None,
    limit: LimitOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    verbose: VerboseOption = False,
    require_curve_quote: bool | None = typer.Option(
        None,
        "--require-curve-quote/--allow-missing-curve-quote",
        help="Override Curve quote strictness for prepare/send.",
    ),
) -> None:
    del verbose
    require_no_confirmation_for_json(json_output=json_output, no_confirmation=no_confirmation)
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    normalized_source_type = _normalize_source_type_filter(source_type)
    normalized_source_address = normalize_cli_address(source_address, param_hint="--source")
    normalized_auction_address = normalize_cli_address(auction_address, param_hint="--auction")
    try:
        if json_output:
            exec_ctx = cli_ctx.resolve_execution(
                required=True,
                required_for="kick execution",
                keystore_path=keystore,
                password_file=password_file,
            )
        else:
            with progress_status("Resolving operator context..."):
                exec_ctx = cli_ctx.resolve_execution(
                    required=True,
                    required_for="kick execution",
                    keystore_path=keystore,
                    password_file=password_file,
                )
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "sourceType": normalized_source_type,
        "sourceAddress": normalized_source_address,
        "auctionAddress": normalized_auction_address,
        "limit": limit,
        "sender": exec_ctx.sender,
        "includeLiveInspection": False,
    }
    preview_fee_context: dict[str, float] | None = None
    local_warnings: list[str] = []
    try:
        with cli_ctx.control_plane_client(auth=True) as client:
            if json_output:
                inspect_response = client.inspect_kicks(payload)
            else:
                with progress_status("Loading kick candidates..."):
                    inspect_response = client.inspect_kicks(payload)
            inspect_result = _inspect_result_from_api(inspect_response["data"])
            review_candidates = _candidate_review_queue(
                inspect_result,
                include_deferred_same_auction=not no_confirmation,
            )
            broadcast_records: list[dict[str, object]] = []
            prepared_actions: list[dict[str, object]] = []
            prepared_candidate_count = 0
            skipped_confirmation_count = 0
            prepare_feedback_emitted = False
            broadcast_feedback_emitted = False
            sent_transaction = False
            short_circuit_same_auction_feedback_emitted = False

            if review_candidates:
                total_candidates = len(review_candidates)
                for index, candidate in enumerate(review_candidates, start=1):
                    prepare_payload = _candidate_prepare_payload(candidate, sender=exec_ctx.sender)
                    if require_curve_quote is not None:
                        prepare_payload["requireCurveQuote"] = require_curve_quote
                    if json_output:
                        prepare_response = client.prepare_kicks(prepare_payload)
                    else:
                        with progress_status(f"Preparing kick {index} of {total_candidates}..."):
                            prepare_response = client.prepare_kicks(prepare_payload)
                    prepared_at_monotonic = _current_monotonic()
                    prepared_data = prepare_response["data"]
                    prepared_actions.append(prepared_data)
                    warnings = list(prepare_response.get("warnings") or [])
                    transactions = list(prepared_data.get("transactions") or [])

                    if prepare_response["status"] == "noop" or not transactions:
                        if not json_output:
                            skip_entries = _prepare_skips(prepared_data, candidate=candidate)
                            if skip_entries or warnings:
                                prepare_feedback_emitted = True
                            for skip in skip_entries:
                                render_skip_panel(
                                    reason=str(skip["reason"]),
                                    token_symbol=skip["token_symbol"],
                                    want_symbol=skip["want_symbol"],
                                    source_name=skip["source_name"],
                                    source_address=skip["source_address"],
                                    auction_address=skip["auction_address"],
                                )
                            render_warnings(warnings)
                            remaining_candidates = review_candidates[index:]
                            if (
                                normalized_source_type == "fee_burner"
                                and _should_stop_after_same_auction_skip(
                                    candidate=candidate,
                                    skip_entries=skip_entries,
                                    remaining_candidates=remaining_candidates,
                                )
                            ):
                                typer.echo("Ending review for the remaining same-auction candidates.")
                                short_circuit_same_auction_feedback_emitted = True
                                break
                        continue

                    prepared_candidate_count += 1
                    if not json_output:
                        if preview_fee_context is None:
                            with progress_status("Loading network fee preview..."):
                                preview_fee_context = _resolve_preview_fee_context(cli_ctx)
                        summary = _kick_submission_summary(
                            prepared_data,
                            candidate=candidate,
                            single_title=f"Kick ({index} of {total_candidates})",
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
                                heading=f"Prepared kick action ({index}/{total_candidates})",
                            )
                        render_warnings(warnings)
                    if not no_confirmation and not typer.confirm("Send this transaction?", default=False):
                        skipped_confirmation_count += 1
                        continue
                    if _prepared_action_is_stale(
                        prepared_at_monotonic=prepared_at_monotonic,
                        max_age_seconds=cli_ctx.settings.prepared_action_max_age_seconds,
                    ):
                        skipped_confirmation_count += 1
                        warning = _prepared_action_stale_warning(
                            cli_ctx.settings.prepared_action_max_age_seconds
                        )
                        local_warnings.append(warning)
                        if not json_output:
                            render_warnings([warning])
                        continue
                    if exec_ctx.signer is None or exec_ctx.sender is None:
                        raise typer.Exit(code=1)
                    if json_output:
                        action_records = execute_prepared_action_sync(
                            settings=cli_ctx.settings,
                            client=client,
                            action_id=str(prepared_data["actionId"]),
                            sender=exec_ctx.sender,
                            signer=exec_ctx.signer,
                            transactions=transactions,
                        )
                    else:
                        typer.echo()
                        with submission_progress("Submitting transaction...") as update_progress:
                            action_records = execute_prepared_action_sync(
                                settings=cli_ctx.settings,
                                client=client,
                                action_id=str(prepared_data["actionId"]),
                                sender=exec_ctx.sender,
                                signer=exec_ctx.signer,
                                transactions=transactions,
                                progress_callback=update_progress,
                            )
                    if not json_output and action_records:
                        render_broadcast_result(action_records)
                        broadcast_feedback_emitted = True
                    broadcast_records.extend(action_records)
                    if action_records:
                        sent_transaction = True
                        break
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    status = "ok" if broadcast_records else "noop"

    if json_output:
        output: dict[str, object] = {
            "inspection": asdict(inspect_result),
            "preparedActions": prepared_actions,
            "broadcastRecords": broadcast_records,
        }
        emit_json("kick.run", status=status, data=output, warnings=local_warnings)
    else:
        if broadcast_records:
            if not broadcast_feedback_emitted:
                render_broadcast_result(broadcast_records)
            if sent_transaction and len(review_candidates) > 1:
                typer.echo("Kick transaction sent. Ending run after the first submitted candidate.")
        elif inspect_result.ready_count == 0:
            typer.echo("No ready kick candidates.")
        elif short_circuit_same_auction_feedback_emitted:
            pass
        elif prepared_candidate_count == 0 and prepare_feedback_emitted:
            pass
        elif prepared_candidate_count > 0 and skipped_confirmation_count == prepared_candidate_count:
            typer.echo("All prepared kick transactions were skipped.")
        else:
            typer.echo("No kick transactions were sent.")

    if status == "noop":
        raise typer.Exit(code=2)
