"""Shared CLI rendering helpers."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import typer
from eth_utils import to_checksum_address
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from tidal.logging import OutputMode
from tidal.normalizers import short_address
from tidal.ops.kick_inspect import KickInspectEntry, KickInspectResult
from tidal.ops.logs import KickLogRecord, RunDetail, ScanRunRecord, ScanRunDetail, TxnRunDetail
from tidal.transaction_service.types import SourceType


@dataclass(slots=True)
class BroadcastRecord:
    operation: str | None
    sender: str | None
    tx_hash: str
    broadcast_at: str | None
    chain_id: int | None = None
    receipt_status: str | None = None
    block_number: int | None = None
    gas_used: int | None = None
    gas_estimate: int | None = None


def _console(*, stderr: bool = False) -> Console:
    width = max(shutil.get_terminal_size((120, 20)).columns, 120)
    return Console(
        file=sys.stderr if stderr else sys.stdout,
        width=width,
        highlight=False,
        soft_wrap=True,
        emoji=False,
    )


def _panel_text(lines: list[str]) -> Text:
    body = Text()
    for index, line in enumerate(lines):
        if index:
            body.append("\n")
        stripped = line.strip()
        style = None
        if stripped and (not line.startswith("  ") or stripped.endswith(("details", "plan", "preview"))):
            style = "bold"
        body.append(line, style=style)
    return body


def render_panel(
    title: str,
    lines: list[str],
    *,
    border_style: str = "cyan",
    stderr: bool = False,
) -> None:
    if not lines:
        return
    _console(stderr=stderr).print(
        Panel.fit(
            _panel_text(lines),
            title=title,
            border_style=border_style,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


def render_skip_panel(
    *,
    reason: str,
    token_symbol: str | None,
    want_symbol: str | None,
    source_name: str | None,
    source_address: str | None,
    auction_address: str | None,
) -> None:
    pair_left = token_symbol or "?"
    pair_right = want_symbol or "?"
    lines = [reason]
    lines.append(f"  Pair:        {pair_left} -> {pair_right}")

    if source_name and source_address and source_name != source_address:
        lines.append(f"  Source:      {source_name} ({short_address(source_address)})")
    elif source_name:
        lines.append(f"  Source:      {source_name}")
    elif source_address:
        lines.append(f"  Source:      {source_address}")

    if auction_address:
        lines.append(f"  Auction:     {auction_address}")

    render_panel("Skip", lines, border_style="yellow")


def _display_bool(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def emit_json(command: str, *, status: str, data: Any, warnings: list[str] | None = None) -> None:
    typer.echo(
        json.dumps(
            {
                "command": command,
                "status": status,
                "warnings": warnings or [],
                "data": _jsonable(data),
            },
            indent=2,
            sort_keys=True,
        )
    )


def kick_scope_label(
    source_type: SourceType | None,
    *,
    source_address: str | None = None,
    auction_address: str | None = None,
) -> str:
    type_label = source_type.replace("_", "-") if source_type else ""
    scope = f"{type_label} candidates" if type_label else "candidates"
    filters: list[str] = []
    if source_address:
        filters.append(f"source {short_address(source_address)}")
    if auction_address:
        filters.append(f"auction {short_address(auction_address)}")
    if not filters:
        return scope
    return f"{scope} for {' and '.join(filters)}"


def _display_address(address: Any) -> str:
    if address is None:
        return "-"
    try:
        return to_checksum_address(str(address))
    except Exception:
        return str(address)


def _display_gas_value(value: Any, *, suffix: str | None = None) -> str:
    if value is None:
        return "unavailable"
    if suffix:
        return f"{int(value):,}{suffix}"
    return f"{int(value):,}"


def _format_decimal_amount(value: Decimal) -> str:
    return f"{float(value):,.4f}" if value < 1 else f"{float(value):,.2f}"


def _safe_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def render_warning_panel(warnings: list[str]) -> None:
    if not warnings:
        return
    render_panel("Warnings", [f"- {warning}" for warning in warnings], border_style="yellow")

def render_status_panel(title: str, message: str | list[str], *, border_style: str) -> None:
    lines = [message] if isinstance(message, str) else message
    render_panel(title, lines, border_style=border_style)


def _format_match_line(match: dict[str, Any]) -> str:
    auction = short_address(str(match.get("auction_address") or match.get("auctionAddress") or "-"))
    factory = short_address(str(match.get("factory_address") or match.get("factoryAddress") or "-"))
    starting_price = match.get("starting_price") if match.get("starting_price") is not None else match.get("startingPrice")
    version = match.get("version") or "unknown"
    return f"auction={auction} factory={factory} startingPrice={starting_price or 'unknown'} version={version}"


def _format_selected_tokens(selected_tokens: list[str], probes: list[dict[str, Any]]) -> str | None:
    if not selected_tokens:
        return None
    selected_set = {str(address).lower() for address in selected_tokens}
    labels: list[str] = []
    for probe in probes:
        token_address = str(probe.get("token_address") or probe.get("tokenAddress") or "").lower()
        if token_address not in selected_set:
            continue
        symbol = probe.get("symbol")
        label = str(symbol or short_address(str(probe.get("token_address") or probe.get("tokenAddress"))))
        labels.append(label)
    if not labels:
        labels = [short_address(str(address)) for address in selected_tokens]
    if len(labels) > 4:
        return ", ".join(labels[:4]) + f", +{len(labels) - 4} more"
    return ", ".join(labels)


def _prepared_action_detail_lines(action_type: str | None, preview: dict[str, Any]) -> list[str]:
    normalized_action_type = (action_type or "").replace("-", "_")
    if normalized_action_type == "deploy":
        lines = [
            "",
            "  Review details",
            f"  Auction:     {_display_address(preview.get('predictedAuctionAddress'))}",
            f"  Want:        {_display_address(preview.get('want'))}",
            f"  Receiver:    {_display_address(preview.get('receiver'))}",
            f"  Start price: {_format_broadcast_value(preview.get('startingPrice'))}",
        ]
        predicted_exists = preview.get("predictedAuctionAddressExists")
        if predicted_exists is not None:
            lines.append(f"  Exists now:  {_display_bool(predicted_exists)}")
        existing_matches = preview.get("existingMatches")
        if isinstance(existing_matches, list):
            lines.append(f"  Matches:     {len(existing_matches)}")
            for match in existing_matches[:3]:
                if isinstance(match, dict):
                    lines.append(f"    - {_format_match_line(match)}")
            if len(existing_matches) > 3:
                lines.append(f"    - +{len(existing_matches) - 3} more")
        return lines

    if normalized_action_type == "enable_tokens":
        inspection = preview.get("inspection") if isinstance(preview.get("inspection"), dict) else {}
        selected_tokens = [str(value) for value in preview.get("selectedTokens") or [] if value]
        probes = [item for item in preview.get("probes") or [] if isinstance(item, dict)]
        lines = [
            "",
            "  Review details",
            f"  Auction:     {_display_address(inspection.get('auction_address') or inspection.get('auctionAddress'))}",
        ]
        selected_label = _format_selected_tokens(selected_tokens, probes)
        if selected_label:
            lines.append(f"  Tokens:      {selected_label}")
        else:
            lines.append(f"  Tokens:      {len(selected_tokens)} token(s)")
        return lines

    if normalized_action_type == "settle":
        inspection = preview.get("inspection") if isinstance(preview.get("inspection"), dict) else {}
        decision = preview.get("decision") if isinstance(preview.get("decision"), dict) else {}
        lines = [
            "",
            "  Review details",
            f"  Auction:     {_display_address(inspection.get('auction_address') or inspection.get('auctionAddress'))}",
            f"  Operation:   {str(decision.get('operation_type') or decision.get('operationType') or '-').replace('_', '-')}",
            f"  Token:       {_display_address(decision.get('token_address') or decision.get('tokenAddress'))}",
            f"  Reason:      {decision.get('reason') or '-'}",
        ]
        return lines

    prepared_operations = preview.get("preparedOperations")
    if isinstance(prepared_operations, list) and prepared_operations:
        lines = ["", "  Planned operations"]
        for index, operation in enumerate(prepared_operations[:3], 1):
            if not isinstance(operation, dict):
                continue
            lines.append(
                f"  {index}. {operation.get('operation') or 'operation'} -> "
                f"{_display_address(operation.get('auctionAddress') or operation.get('to') or operation.get('sourceAddress'))}"
            )
        if len(prepared_operations) > 3:
            lines.append(f"  +{len(prepared_operations) - 3} more operation(s)")
        return lines

    return []


def _prepared_action_transaction_lines(transactions: list[dict[str, Any]]) -> list[str]:
    lines = ["", "  Send details"]
    single_transaction = len(transactions) == 1
    for index, tx in enumerate(transactions, 1):
        prefix = "  " if single_transaction else "    "
        lines.extend(
            [
                *( [f"  Tx {index}:        {tx.get('operation') or 'transaction'}"] if not single_transaction else [] ),
                f"{prefix}From:        {_display_address(tx.get('sender'))}",
                f"{prefix}Gas est:     {_display_gas_value(tx.get('gasEstimate'))}",
                f"{prefix}Gas limit:   {_display_gas_value(tx.get('gasLimit'))}",
            ]
        )
    return lines


def render_prepared_action_summary(data: dict[str, Any], *, heading: str = "Prepared Action") -> None:
    action_type = str(data.get("actionType") or "").replace("_", "-") or None
    preview = data.get("preview")
    transactions = data.get("transactions") or []
    transaction_count = len(transactions) if isinstance(transactions, list) else 0
    transaction_label = "transaction" if transaction_count == 1 else "transactions"
    lines = [f"  {(action_type or 'action')} · {transaction_count} {transaction_label}"]
    if isinstance(preview, dict):
        lines.extend(_prepared_action_detail_lines(action_type, preview))
    if isinstance(transactions, list) and transactions:
        lines.extend(_prepared_action_transaction_lines([tx for tx in transactions if isinstance(tx, dict)]))
    render_panel(heading, lines, border_style="cyan")


def render_kick_submission_summary(summary: dict[str, Any]) -> None:
    kicks = summary["kicks"]
    batch_size = summary["batch_size"]
    gas_cost_eth = summary.get("gas_cost_eth")
    priority_fee = summary.get("priority_fee_gwei", 0)
    max_fee = summary.get("max_fee_per_gas_gwei", 0)
    gas_estimate = summary.get("gas_estimate")

    try:
        max_fee_str = f"{float(max_fee):.2f}"
    except (TypeError, ValueError):
        max_fee_str = str(max_fee)

    if batch_size == 1:
        k = kicks[0]
        sender = k.get("sender")
        source_name = k.get("source_name") or "Unknown"
        token_sym = k.get("token_symbol") or "???"
        want_sym = k.get("want_symbol") or "???"
        profile_name = k.get("pricing_profile_name") or "default"
        amount = Decimal(str(k["sell_amount"]))
        amount_str = _format_decimal_amount(amount)
        quote_amount = Decimal(str(k["quote_amount"]))
        usd_value = Decimal(str(k["usd_value"]))

        starting_price = Decimal(str(k["starting_price"]))
        minimum_price = Decimal(str(k["minimum_price"]))
        step_decay_rate_bps = k.get("step_decay_rate_bps")
        step_decay_str = f"{step_decay_rate_bps / 100:.2f}%" if step_decay_rate_bps is not None else "—"
        quote_amount_str = _format_decimal_amount(quote_amount)
        quote_value_line = None
        divergence_line = None
        want_price_usd = k.get("want_price_usd")
        if want_price_usd is not None:
            try:
                want_price = Decimal(str(want_price_usd))
                if want_price > 0:
                    quote_value_usd = quote_amount * want_price
                    quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym} (~${float(quote_value_usd):,.2f})"
                    if usd_value > 0 and quote_amount > 0:
                        spot_quote_amount = usd_value / want_price
                        if spot_quote_amount > 0:
                            deviation_ratio = (quote_amount - spot_quote_amount) / spot_quote_amount
                            threshold_pct = Decimal(str(summary.get("quote_spot_warning_threshold_pct", 2)))
                            threshold_ratio = threshold_pct / Decimal("100")
                            if abs(deviation_ratio) >= threshold_ratio:
                                direction = "higher" if deviation_ratio > 0 else "lower"
                                spot_quote_amount_str = _format_decimal_amount(spot_quote_amount)
                                divergence_line = (
                                    f"⚠️  Warning: live quote is {float(abs(deviation_ratio) * 100):,.1f}% "
                                    f"{direction} than evaluated spot ({quote_amount_str} {want_sym} quoted vs "
                                    f"{spot_quote_amount_str} {want_sym} at spot)"
                                )
                else:
                    quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym}"
            except (InvalidOperation, ValueError, TypeError):
                quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym}"
        else:
            quote_value_line = f"  Quote out:   {quote_amount_str} {want_sym}"

        quote_rate = _safe_decimal(k.get("quote_rate"))
        start_rate = _safe_decimal(k.get("start_rate"))
        floor_rate = _safe_decimal(k.get("floor_rate"))

        rate_line = None
        if amount > 0:
            quote_rate = quote_rate if quote_rate is not None else quote_amount / amount
            start_rate = start_rate if start_rate is not None else starting_price / amount
            floor_rate = floor_rate if floor_rate is not None else minimum_price / amount
            rate_line = (
                f"  Rate:        {float(quote_rate):,.4f} quoted | {float(start_rate):,.4f} start | "
                f"{float(floor_rate):,.4f} floor {want_sym}/{token_sym}"
            )
        precision_line = None
        if quote_amount > 0 and starting_price > quote_amount * 2:
            precision_line = f"               ↳ ceiled lot based on {quote_amount:.4f} quote"

        content = []
        if divergence_line:
            content.extend([divergence_line, ""])
        content.extend([
            str(summary.get("single_title") or "Kick (1 of 1)"),
            "",
            "  Auction details",
            f"  Source:      {source_name} ({short_address(_display_address(k['source']))})",
            f"  Auction:     {_display_address(k['auction'])}",
            f"  Sell amount: {amount_str} {token_sym} (~${float(usd_value):,.2f})",
            quote_value_line,
            f"  Start quote: {k['starting_price_display']}",
            f"  Min quote:   {k.get('minimum_quote_display') or k.get('minimum_price_display') or '-'}",
            f"  Profile:     {profile_name} | decay {step_decay_str}",
        ])
        if rate_line:
            content.append(rate_line)
        if precision_line:
            content.append(precision_line)
        content.extend([
            "",
            "  Send details",
            f"  From:        {_display_address(sender)}",
            (
                f"  Gas est:     {_display_gas_value(gas_estimate)} (~{float(gas_cost_eth):.6f} ETH)"
                if gas_estimate is not None and gas_cost_eth is not None
                else "  Gas est:     unavailable"
            ),
            f"  Gas limit:   {_display_gas_value(summary.get('gas_limit'))}",
            f"  Base fee:    {summary.get('base_fee_gwei', 0):.2f} gwei",
            f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee_str} gwei",
        ])
    else:
        content = [f"Batch of {batch_size} kicks", ""]
        for index, kick in enumerate(kicks, 1):
            source_name = kick.get("source_name") or "Unknown"
            token_sym = kick.get("token_symbol") or "???"
            profile_name = kick.get("pricing_profile_name") or "default"
            amount = Decimal(str(kick["sell_amount"]))
            amount_str = _format_decimal_amount(amount)
            usd_value = float(kick["usd_value"])
            content.append(f"  {index}. {source_name} | {amount_str} {token_sym} (~${usd_value:,.2f}) | {profile_name}")

        total_usd = float(summary["total_usd"])
        content.extend([
            "",
            f"  Total USD:   ~${total_usd:,.2f}",
            (
                f"  Gas est:     {_display_gas_value(gas_estimate)} (~{float(gas_cost_eth):.6f} ETH)"
                if gas_estimate is not None and gas_cost_eth is not None
                else "  Gas est:     unavailable"
            ),
            f"  Fees:        priority {priority_fee:.2f} gwei | max {max_fee_str} gwei",
        ])

    render_panel("Prepared Transaction", content, border_style="cyan")


def render_scan_summary(result: Any) -> None:
    typer.echo(
        (
            f"scan_complete run_id={result.run_id} status={result.status} "
            f"strategies={result.strategies_seen} pairs={result.pairs_seen} "
            f"succeeded={result.pairs_succeeded} failed={result.pairs_failed}"
        )
    )


def _format_broadcast_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_broadcast_records(records: list[BroadcastRecord]) -> None:
    if not records:
        return

    for index, record in enumerate(records, 1):
        receipt_status = str(record.receipt_status or "").upper()
        if receipt_status == "CONFIRMED":
            status_title = "Confirmed"
            border_style = "green"
        elif receipt_status in {"FAILED", "REVERTED"}:
            status_title = "Failed"
            border_style = "red"
        else:
            status_title = "Pending Confirmation"
            border_style = "yellow"
        heading = status_title if len(records) == 1 else f"Transaction {index} · {status_title}"
        lines: list[str] = []
        if record.operation:
            lines.append(f"  Operation:    {record.operation}")
        lines.append(f"  Sender:       {_format_broadcast_value(record.sender)}")
        lines.append(f"  Tx hash:      {record.tx_hash}")
        lines.append(f"  Broadcast at: {_format_broadcast_value(record.broadcast_at)}")
        if record.receipt_status is not None:
            lines.append(f"  Receipt:      {record.receipt_status}")
        else:
            lines.append("  Receipt:      pending")
        render_panel(heading, lines, border_style=border_style)


def kick_broadcast_records(run_rows: list[dict[str, object]], *, sender: str | None) -> list[BroadcastRecord]:
    records: list[BroadcastRecord] = []
    seen_tx_hashes: set[str] = set()
    for row in run_rows:
        tx_hash = row.get("tx_hash")
        if not tx_hash:
            continue
        tx_hash_str = str(tx_hash)
        if tx_hash_str in seen_tx_hashes:
            continue
        seen_tx_hashes.add(tx_hash_str)
        operation_type = str(row.get("operation_type") or "kick").replace("_", "-")
        block_number = row.get("block_number")
        gas_used = row.get("gas_used")
        gas_estimate = row.get("gas_estimate")
        records.append(
            BroadcastRecord(
                operation=operation_type,
                sender=sender,
                tx_hash=tx_hash_str,
                broadcast_at=str(row.get("created_at")) if row.get("created_at") else None,
                chain_id=int(row["chain_id"]) if row.get("chain_id") is not None else None,
                receipt_status=str(row.get("status")) if row.get("status") else None,
                block_number=int(block_number) if block_number is not None else None,
                gas_used=int(gas_used) if gas_used is not None else None,
                gas_estimate=int(gas_estimate) if gas_estimate is not None else None,
            )
        )
    return records


def render_kick_run_summary(
    *,
    result: Any,
    live: bool,
    source_type: SourceType | None,
    source_address: str | None,
    auction_address: str | None,
    run_rows: list[dict[str, object]],
    verbose: bool,
    sender: str | None = None,
    show_broadcast_records: bool = True,
) -> None:
    statuses = {str(row["status"]) for row in run_rows}
    skipped_count = sum(1 for row in run_rows if str(row.get("status")) == "USER_SKIPPED")
    type_label = source_type.replace("_", "-") if source_type else None
    eligible_candidates_found = getattr(result, "eligible_candidates_found", None)
    deferred_same_auction_count = getattr(result, "deferred_same_auction_count", 0)
    limited_candidate_count = getattr(result, "limited_candidate_count", 0)

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
        typer.echo("Pending confirmation.")
    elif result.kicks_failed:
        typer.echo("Failed.")
    else:
        typer.echo("Completed.")

    typer.echo(f"Run ID:       {result.run_id}")
    if type_label:
        typer.echo(f"Type:         {type_label}")
    if source_address:
        typer.echo(f"Source:       {source_address}")
    if auction_address:
        typer.echo(f"Auction:      {auction_address}")
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
    if limited_candidate_count:
        typer.echo(f"Limited:      {limited_candidate_count}")

    detailed_failure_rows = [row for row in run_rows if row.get("error_message")]
    if len(detailed_failure_rows) == 1:
        typer.echo(f"Failure:      {detailed_failure_rows[0]['error_message']}")

    detailed_rows = [row for row in run_rows if row.get("operation_type", "kick") == "kick"]
    if len(detailed_rows) == 1:
        quote_response_json = detailed_rows[0].get("quote_response_json")
        quote_url = None
        if quote_response_json:
            try:
                payload = json.loads(str(quote_response_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and payload.get("requestUrl"):
                quote_url = str(payload["requestUrl"])
        if quote_url:
            typer.echo("Quote URL:")
            typer.echo(quote_url)

    if live and show_broadcast_records:
        broadcast_records = kick_broadcast_records(run_rows, sender=sender)
        if broadcast_records:
            typer.echo()
            render_broadcast_records(broadcast_records)

    if verbose and result.failure_summary:
        typer.echo("Failure summary:")
        for message, count in sorted(result.failure_summary.items(), key=lambda item: (-item[1], item[0])):
            typer.echo(f"  {count}x {message}")


def _format_inspect_entry(entry: KickInspectEntry) -> str:
    symbol = entry.token_symbol or "UNKNOWN"
    source_name = entry.source_name or entry.source_address
    line = (
        f"  - {entry.state:<21} {symbol:<10} ${entry.usd_value:,.2f} "
        f"{short_address(entry.source_address)} -> {short_address(entry.auction_address)}"
    )
    if entry.detail:
        line += f" | {entry.detail}"
    if entry.auction_active is True and entry.active_token:
        line += f" | active={short_address(entry.active_token)}"
    elif entry.auction_active is True:
        line += " | active=yes"
    elif entry.auction_active is False:
        line += " | active=no"
    if source_name != entry.source_address:
        line += f" | {source_name}"
    return line


def render_kick_inspect(result: KickInspectResult, *, show_all: bool) -> None:
    typer.echo("Kick inspect:")
    if result.source_address:
        typer.echo(f"  source       {result.source_address}")
    if result.auction_address:
        typer.echo(f"  auction      {result.auction_address}")
    if result.source_type:
        typer.echo(f"  type         {result.source_type.replace('_', '-')}")
    if result.limit:
        typer.echo(f"  limit        {result.limit}")
    typer.echo(f"  eligible     {result.eligible_count}")
    typer.echo(f"  selected     {result.selected_count}")
    typer.echo(f"  ready        {result.ready_count}")
    typer.echo(f"  cooldown     {result.cooldown_count}")
    typer.echo(f"  deferred     {result.deferred_same_auction_count}")
    typer.echo(f"  limited      {result.limited_count}")

    sections: list[tuple[str, list[KickInspectEntry]]] = [
        ("Ready", result.ready),
        ("Cooldown", result.cooldown_skips),
        ("Deferred", result.deferred_same_auction),
        ("Limited", result.limited),
    ]
    for heading, entries in sections:
        if not entries:
            continue
        if not show_all and heading not in {"Ready", "Cooldown"}:
            continue
        typer.echo()
        typer.echo(f"{heading}:")
        for entry in entries:
            typer.echo(_format_inspect_entry(entry))


def render_kick_logs(records: list[KickLogRecord]) -> None:
    if not records:
        typer.echo("No kick records found.")
        return
    for record in records:
        token_label = record.token_symbol or short_address(record.token_address)
        usd_label = f"${record.usd_value}" if record.usd_value is not None else "-"
        typer.echo(
            f"{record.created_at} {record.status:<15} {token_label:<12} {usd_label:<12} "
            f"{short_address(record.auction_address)} run={record.run_id}"
        )
        if record.error_message:
            typer.echo(f"  error: {record.error_message}")
        if record.quote_url:
            typer.echo(f"  quote: {record.quote_url}")


def render_scan_runs(records: list[ScanRunRecord]) -> None:
    if not records:
        typer.echo("No scan runs found.")
        return
    for record in records:
        typer.echo(
            f"{record.started_at} {record.status:<15} run={record.run_id} "
            f"pairs={record.pairs_seen} ok={record.pairs_succeeded} failed={record.pairs_failed} errors={record.error_count}"
        )
        if record.error_summary:
            typer.echo(f"  summary: {record.error_summary}")


def render_run_detail(detail: RunDetail) -> None:
    if isinstance(detail, TxnRunDetail):
        typer.echo("Kick run:")
        typer.echo(f"  run id       {detail.run_id}")
        typer.echo(f"  status       {detail.status}")
        typer.echo(f"  live         {'yes' if detail.live else 'no'}")
        typer.echo(f"  started      {detail.started_at}")
        typer.echo(f"  finished     {detail.finished_at or '-'}")
        typer.echo(f"  candidates   {detail.candidates_found}")
        typer.echo(f"  attempted    {detail.kicks_attempted}")
        typer.echo(f"  succeeded    {detail.kicks_succeeded}")
        typer.echo(f"  failed       {detail.kicks_failed}")
        if detail.error_summary:
            typer.echo(f"  summary      {detail.error_summary}")
        if detail.records:
            typer.echo()
            typer.echo("Attempts:")
            for record in detail.records:
                token_label = record.token_symbol or short_address(record.token_address)
                typer.echo(
                    f"  - {record.status:<15} {token_label:<12} {record.auction_address} created={record.created_at}"
                )
                if record.error_message:
                    typer.echo(f"    error: {record.error_message}")
                if record.tx_hash:
                    typer.echo(f"    tx:    {record.tx_hash}")
                if record.quote_url:
                    typer.echo(f"    quote: {record.quote_url}")
        return

    typer.echo("Scan run:")
    typer.echo(f"  run id       {detail.run_id}")
    typer.echo(f"  status       {detail.status}")
    typer.echo(f"  started      {detail.started_at}")
    typer.echo(f"  finished     {detail.finished_at or '-'}")
    typer.echo(f"  vaults       {detail.vaults_seen}")
    typer.echo(f"  strategies   {detail.strategies_seen}")
    typer.echo(f"  pairs        {detail.pairs_seen}")
    typer.echo(f"  succeeded    {detail.pairs_succeeded}")
    typer.echo(f"  failed       {detail.pairs_failed}")
    if detail.error_summary:
        typer.echo(f"  summary      {detail.error_summary}")
    if detail.errors:
        typer.echo()
        typer.echo("Errors:")
        for error in detail.errors:
            typer.echo(
                f"  - {error.stage}/{error.error_code} source={error.source_address or '-'} "
                f"token={error.token_address or '-'}"
            )
            typer.echo(f"    {error.error_message}")
