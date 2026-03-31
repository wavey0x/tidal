"""Auction operator CLI commands."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import typer
from eth_utils import to_checksum_address

from tidal.auction_settlement import (
    build_auction_settlement_call,
    decide_auction_settlement,
    format_operation_type,
    inspect_auction_settlement,
)
from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_exit_codes import EXECUTION_ERROR, NOOP
from tidal.cli_options import (
    AccountOption,
    BroadcastOption,
    BypassConfirmationOption,
    ConfigOption,
    JsonOption,
    KeystoreOption,
    PasswordFileOption,
    SenderOption,
)
from tidal.cli_renderers import (
    BroadcastRecord,
    emit_json,
    format_settlement_reason_lines,
    format_warning_lines,
    render_broadcast_records,
)
from tidal.errors import ConfigurationError
from tidal.logging import OutputMode, configure_logging
from tidal.ops.auction_enable import (
    AuctionInspection,
    AuctionTokenEnabler,
    TokenProbe,
    format_probe_reason,
)
from tidal.ops.deploy import (
    build_default_salt,
    default_factory_address,
    default_governance_address,
    preview_deployment,
    read_token_symbol,
    resolve_starting_price,
    send_live_deployment,
    summarize_matches,
)
from tidal.runtime import build_web3_client
from tidal.time import utcnow_iso
from tidal.transaction_service.kicker import _DEFAULT_PRIORITY_FEE_GWEI, _format_execution_error

app = typer.Typer(help="Auction operator commands", no_args_is_help=True)


def _normalize_address_list(values: list[str] | None, *, param_hint: str) -> list[str]:
    return [normalize_cli_address(value, param_hint=param_hint) for value in values or []]


def _print_auction_summary(inspection: AuctionInspection) -> None:
    typer.echo("Auction summary:")
    typer.echo(f"  auction       {to_checksum_address(inspection.auction_address)}")
    typer.echo(f"  governance    {to_checksum_address(inspection.governance)}")
    typer.echo(f"  receiver      {to_checksum_address(inspection.receiver)}")
    typer.echo(f"  want          {to_checksum_address(inspection.want)}")
    typer.echo(f"  version       {inspection.version or 'unknown'}")
    typer.echo(f"  in factory    {'yes' if inspection.in_configured_factory else 'no'}")
    typer.echo(f"  yearn gov     {'yes' if inspection.governance_matches_required else 'no'}")
    typer.echo(f"  enabled now   {len(inspection.enabled_tokens)} token(s)")
    typer.echo()


def _print_probe_table(probes: list[TokenProbe]) -> None:
    if not probes:
        typer.echo("No candidate tokens were discovered.")
        typer.echo()
        return

    typer.echo("Token probe results:")
    for index, probe in enumerate(probes, 1):
        balance = probe.normalized_balance if probe.normalized_balance is not None else "-"
        origins = ",".join(probe.origins) if probe.origins else "-"
        detail = f" ({probe.detail})" if probe.detail else ""
        typer.echo(
            f"  [{index:02d}] {probe.status:<8} {probe.display_label} | "
            f"balance={balance} | origins={origins} | {format_probe_reason(probe.reason)}{detail}"
        )
    typer.echo()


def _render_deploy_preview(preview) -> None:
    typer.echo("Deployment parameters:")
    typer.echo(f"  factory       {to_checksum_address(preview.factory_address)}")
    typer.echo(f"  want          {to_checksum_address(preview.want)}")
    typer.echo(f"  receiver      {to_checksum_address(preview.receiver)}")
    typer.echo(f"  governance    {to_checksum_address(preview.governance)}")
    typer.echo(f"  startingPrice {preview.starting_price}")
    typer.echo(f"  salt          {preview.salt}")
    if preview.sender_address:
        typer.echo(f"  sender        {to_checksum_address(preview.sender_address)}")
    typer.echo()
    if preview.existing_matches:
        typer.echo("Existing matching auctions:")
        for line in summarize_matches(preview.existing_matches):
            typer.echo(f"  - {line}")
    else:
        typer.echo(summarize_matches(preview.existing_matches)[0])
    if preview.predicted_address:
        typer.echo(f"Predicted auction address: {to_checksum_address(preview.predicted_address)}")
        typer.echo(
            "Predicted auction address already exists in the selected factory."
            if preview.predicted_address_exists
            else "Predicted auction address is not currently deployed in the selected factory."
        )
    if preview.gas_estimate is not None:
        typer.echo(f"Estimated gas: {preview.gas_estimate:,}")
    if preview.preview_error:
        typer.echo(f"Preview call failed: {preview.preview_error}")
    if preview.gas_error:
        typer.echo(f"Gas estimate failed: {preview.gas_error}")


async def _preview_settlement_execution(
    *,
    settings,
    auction_address: str,
    decision,
    sender_address: str | None,
    broadcast: bool,
    signer,
    receipt_timeout: int,
) -> dict[str, object]:
    web3_client = build_web3_client(settings)
    settlement_call = build_auction_settlement_call(
        settings=settings,
        web3_client=web3_client,
        auction_address=auction_address,
        decision=decision,
    )

    result: dict[str, object] = {
        "operation_type": settlement_call.operation_type,
        "auction": to_checksum_address(auction_address),
        "token": to_checksum_address(settlement_call.token_address),
        "sender": to_checksum_address(sender_address) if sender_address else None,
        "target": to_checksum_address(settlement_call.target_address),
        "data": settlement_call.data,
    }

    gas_estimate = None
    gas_limit = None
    warning = None
    if sender_address:
        try:
            gas_estimate = await web3_client.estimate_gas(
                {
                    "from": to_checksum_address(sender_address),
                    "to": to_checksum_address(settlement_call.target_address),
                    "data": settlement_call.data,
                    "chainId": settings.chain_id,
                }
            )
            gas_limit = min(int(gas_estimate * 1.2), settings.txn_max_gas_limit)
        except Exception as exc:  # noqa: BLE001
            warning = f"Gas estimate failed: {_format_execution_error(exc)}"
    else:
        warning = "No sender address available for preview gas estimation."

    base_fee_wei = await web3_client.get_base_fee()
    base_fee_gwei = base_fee_wei / 1e9
    try:
        suggested_priority_fee_wei = await web3_client.get_max_priority_fee()
    except Exception:  # noqa: BLE001
        suggested_priority_fee_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 1e9)
    priority_fee_wei = min(suggested_priority_fee_wei, settings.txn_max_priority_fee_gwei * 10**9)
    result["gas_estimate"] = gas_estimate
    result["gas_limit"] = gas_limit
    result["base_fee_gwei"] = base_fee_gwei
    result["priority_fee_gwei"] = priority_fee_wei / 1e9
    if warning:
        result["warning"] = warning

    if not broadcast:
        return result

    if signer is None:
        raise SystemExit("Signer is required for broadcast settlement execution.")

    tx = {
        "to": to_checksum_address(settlement_call.target_address),
        "data": settlement_call.data,
        "chainId": settings.chain_id,
        "gas": gas_limit or settings.txn_max_gas_limit,
        "maxFeePerGas": int((max(settings.txn_max_base_fee_gwei, base_fee_gwei) + settings.txn_max_priority_fee_gwei) * 10**9),
        "maxPriorityFeePerGas": priority_fee_wei,
        "nonce": await web3_client.get_transaction_count(signer.address),
        "type": 2,
    }
    signed_tx = signer.sign_transaction(tx)
    tx_hash = await web3_client.send_raw_transaction(signed_tx)
    result["broadcast_at"] = utcnow_iso()
    receipt = await web3_client.get_transaction_receipt(tx_hash, timeout_seconds=receipt_timeout)
    result["tx_hash"] = tx_hash
    result["receipt_status"] = "CONFIRMED" if receipt.get("status") == 1 else "REVERTED"
    result["block_number"] = receipt.get("blockNumber")
    result["gas_used"] = receipt.get("gasUsed")
    return result


def _render_settlement_summary(
    *,
    inspection,
    decision,
    execution: dict[str, object] | None,
    warnings: list[str],
    broadcast: bool,
    status: str,
) -> None:
    typer.echo(f"Auction:       {to_checksum_address(inspection.auction_address)}")
    typer.echo(f"Method:        {format_operation_type(decision.operation_type)}")
    for line in format_settlement_reason_lines(decision.reason):
        typer.echo(line)
    typer.echo(f"Active:        {'yes' if inspection.is_active_auction else 'no' if inspection.is_active_auction is False else 'unknown'}")
    typer.echo(f"Active token:  {to_checksum_address(inspection.active_token) if inspection.active_token else '-'}")
    typer.echo(
        "Active tokens: "
        + (", ".join(to_checksum_address(token) for token in inspection.active_tokens) if inspection.active_tokens else "-")
    )
    typer.echo(f"Available:     {inspection.active_available_raw if inspection.active_available_raw is not None else 'unavailable'}")
    typer.echo(f"Live price:    {inspection.active_price_raw if inspection.active_price_raw is not None else 'unavailable'}")
    typer.echo(
        f"Floor price:   {inspection.minimum_price_public_raw if inspection.minimum_price_public_raw is not None else 'unavailable'}"
    )
    typer.echo(
        f"Min price:     "
        f"{inspection.minimum_price_scaled_1e18 if inspection.minimum_price_scaled_1e18 is not None else 'unavailable'}"
        f"{' (scaled 1e18)' if inspection.minimum_price_scaled_1e18 is not None else ''}"
    )

    if execution is not None:
        typer.echo(f"Sender:        {execution.get('sender') or '-'}")
        typer.echo(f"Target:        {execution.get('target') or '-'}")
        typer.echo(f"Data:          {execution.get('data') or '-'}")
        typer.echo(f"Gas estimate:  {execution.get('gas_estimate') or 'unavailable'}")
        typer.echo(f"Gas limit:     {execution.get('gas_limit') or 'unavailable'}")
        typer.echo(f"Base fee:      {float(execution['base_fee_gwei']):.4f} gwei")
        typer.echo(f"Priority fee:  {float(execution['priority_fee_gwei']):.4f} gwei")

    for warning in warnings:
        warning_lines = format_warning_lines(warning, bullet="Warning:       ")
        if len(warning_lines) == 1:
            typer.echo(warning_lines[0])
        else:
            typer.echo(warning_lines[0])
            for continuation in warning_lines[1:]:
                typer.echo(" " * len("Warning:       ") + continuation.strip())

    if decision.status == "noop":
        typer.echo("No settlement action is currently available.")
    elif decision.status == "error":
        typer.echo("Settlement inspection failed.")
    elif not broadcast:
        typer.echo("Transaction status:")
        typer.echo("  Dry run mode enabled.")
        typer.echo("  Use --broadcast to submit transaction on chain.")
    elif status == "noop":
        typer.echo("Aborted before broadcast.")

    if execution is not None and execution.get("tx_hash"):
        typer.echo()
        render_broadcast_records(
            [
                BroadcastRecord(
                    operation=format_operation_type(decision.operation_type),
                    sender=str(execution.get("sender")) if execution.get("sender") else None,
                    tx_hash=str(execution["tx_hash"]),
                    broadcast_at=str(execution.get("broadcast_at")) if execution.get("broadcast_at") else None,
                    receipt_status=str(execution.get("receipt_status")) if execution.get("receipt_status") else None,
                    block_number=int(execution["block_number"]) if execution.get("block_number") is not None else None,
                    gas_used=int(execution["gas_used"]) if execution.get("gas_used") is not None else None,
                    gas_estimate=int(execution["gas_estimate"]) if execution.get("gas_estimate") is not None else None,
                )
            ]
        )


@app.command("deploy")
def deploy(
    want: str = typer.Option(..., "--want", help="Want token address."),
    receiver: str = typer.Option(..., "--receiver", help="Auction receiver address."),
    config: ConfigOption = None,
    factory: str | None = typer.Option(None, "--factory", help="Auction factory address."),
    governance: str | None = typer.Option(None, "--governance", help="Governance / trade handler address."),
    starting_price: int | None = typer.Option(None, "--starting-price", min=0, help="Starting price for the new auction."),
    salt: str | None = typer.Option(None, "--salt", help="Optional deployment salt."),
    broadcast: BroadcastOption = False,
    bypass_confirmation: BypassConfirmationOption = False,
    sender: SenderOption = None,
    account: AccountOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
) -> None:
    """Preview or deploy a single auction from the configured factory."""

    configure_logging(output_mode=OutputMode.TEXT)
    if bypass_confirmation and not broadcast:
        raise typer.BadParameter("--bypass-confirmation requires --broadcast", param_hint="--bypass-confirmation")
    cli_ctx = CLIContext(config, mode="server")
    try:
        w3 = cli_ctx.sync_web3()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    normalized_want = normalize_cli_address(want, param_hint="--want")
    normalized_receiver = normalize_cli_address(receiver, param_hint="--receiver")
    normalized_factory = normalize_cli_address(factory, param_hint="--factory") if factory else default_factory_address(cli_ctx.settings)
    normalized_governance = (
        normalize_cli_address(governance, param_hint="--governance")
        if governance
        else default_governance_address()
    )
    exec_ctx = cli_ctx.resolve_execution(
        broadcast=broadcast,
        required_for="broadcast auction deployment",
        sender=normalize_cli_address(sender, param_hint="--sender"),
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    preview_sender = exec_ctx.sender

    resolved_salt = salt or build_default_salt(normalized_want, normalized_receiver, normalized_governance)
    resolved_starting_price = starting_price
    if resolved_starting_price is None:
        existing_preview = preview_deployment(
            w3,
            cli_ctx.settings,
            factory_address=normalized_factory,
            want=normalized_want,
            receiver=normalized_receiver,
            governance=normalized_governance,
            starting_price=0,
            salt=resolved_salt,
            sender_address=preview_sender,
        )
        try:
            resolved_starting_price = resolve_starting_price(provided=None, matches=existing_preview.existing_matches)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--starting-price") from exc
    initial_preview = preview_deployment(
        w3,
        cli_ctx.settings,
        factory_address=normalized_factory,
        want=normalized_want,
        receiver=normalized_receiver,
        governance=normalized_governance,
        starting_price=resolved_starting_price,
        salt=resolved_salt,
        sender_address=preview_sender,
    )

    status = "ok" if (initial_preview.predicted_address or initial_preview.gas_estimate is not None) else "error"
    warnings: list[str] = []
    want_symbol = read_token_symbol(w3, normalized_want)
    if want_symbol is None:
        warnings.append("Could not resolve token symbol for the want token.")
    if initial_preview.preview_error:
        warnings.append(f"Preview call failed: {initial_preview.preview_error}")
    if initial_preview.gas_error:
        warnings.append(f"Gas estimate failed: {initial_preview.gas_error}")

    execution = None
    if broadcast:
        prompt = (
            "Preview failed. Broadcast deployment anyway?"
            if initial_preview.preview_error or initial_preview.gas_error
            else "Broadcast deployment?"
        )
        if bypass_confirmation or typer.confirm(prompt, default=False):
            if exec_ctx.signer is None:
                raise SystemExit("Signer is required for broadcast deployment.")
            execution = send_live_deployment(
                w3,
                signer=exec_ctx.signer,
                factory_address=initial_preview.factory_address,
                want=initial_preview.want,
                receiver=initial_preview.receiver,
                governance=initial_preview.governance,
                starting_price=initial_preview.starting_price,
                salt=initial_preview.salt,
            )
        else:
            status = "noop"

    execution_data = None
    if execution is not None:
        execution_data = asdict(execution)
        execution_data["sender"] = preview_sender
    data = {
        "preview": asdict(initial_preview),
        "want_symbol": want_symbol,
        "execution": execution_data,
    }
    if json_output:
        emit_json("auction.deploy", status=status, data=data, warnings=warnings)
    else:
        if want_symbol:
            typer.echo(f"Want token symbol: {want_symbol}")
        _render_deploy_preview(initial_preview)
        for warning in warnings:
            typer.echo(f"Warning: {warning}")
        if not broadcast:
            typer.echo("Dry run only. No transaction was sent.")
        elif status == "noop":
            typer.echo("Aborted before broadcast.")
        elif execution is not None:
            typer.echo()
            render_broadcast_records(
                [
                    BroadcastRecord(
                        operation="deploy",
                        sender=preview_sender,
                        tx_hash=execution.tx_hash,
                        broadcast_at=execution.broadcast_at,
                        receipt_status="CONFIRMED" if execution.receipt_status == 1 else "REVERTED",
                        block_number=execution.block_number,
                        gas_used=execution.gas_used,
                        gas_estimate=initial_preview.gas_estimate,
                    )
                ]
            )

    if execution is not None and execution.receipt_status != 1:
        raise typer.Exit(code=EXECUTION_ERROR)
    if status == "error":
        raise typer.Exit(code=EXECUTION_ERROR)
    if status == "noop":
        raise typer.Exit(code=NOOP)


@app.command("enable-tokens")
def enable_tokens(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction address to inspect."),
    config: ConfigOption = None,
    extra_token: list[str] | None = typer.Option(
        None,
        "--extra-token",
        help="Extra token address to probe. Can be supplied multiple times.",
    ),
    broadcast: BroadcastOption = False,
    bypass_confirmation: BypassConfirmationOption = False,
    sender: SenderOption = None,
    account: AccountOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect an auction and queue enable(address) calls for relevant tokens."""

    configure_logging(output_mode=OutputMode.TEXT)
    if bypass_confirmation and not broadcast:
        raise typer.BadParameter("--bypass-confirmation requires --broadcast", param_hint="--bypass-confirmation")
    cli_ctx = CLIContext(config, mode="server")
    normalized_auction_address = normalize_cli_address(auction_address, param_hint="AUCTION")
    normalized_extra_tokens = _normalize_address_list(extra_token, param_hint="--extra-token")
    normalized_sender = normalize_cli_address(sender, param_hint="--sender")
    try:
        w3 = cli_ctx.sync_web3()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    exec_ctx = cli_ctx.resolve_execution(
        broadcast=broadcast,
        required_for="broadcast enable-tokens execution",
        sender=normalized_sender,
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    preview_sender = exec_ctx.sender

    enabler = AuctionTokenEnabler(w3, cli_ctx.settings)
    inspection = enabler.inspect_auction(normalized_auction_address)
    source = enabler.resolve_source(inspection)
    discovery = enabler.discover_tokens(
        inspection=inspection,
        source=source,
        manual_tokens=normalized_extra_tokens,
    )
    probes = enabler.probe_tokens(
        inspection=inspection,
        source=source,
        discovery=discovery,
    )
    eligible = [probe for probe in probes if probe.status == "eligible"]
    selected_addresses = [probe.token_address for probe in eligible]

    if selected_addresses:
        commands, state = enabler.build_enable_plan(
            inspection=inspection,
            tokens=selected_addresses,
        )
        preview = enabler.preview_execution(
            trade_handler_address=inspection.governance,
            commands=commands,
            state=state,
            caller_address=preview_sender,
        )
        preview_authorized = enabler.is_authorized_mech(inspection.governance, preview_sender) if preview_sender else None
    else:
        commands = []
        state = []
        preview = None
        preview_authorized = enabler.is_authorized_mech(inspection.governance, preview_sender) if preview_sender else None

    warnings: list[str] = []
    if not inspection.in_configured_factory:
        warnings.append("Auction is not in the configured factory.")
    if not inspection.governance_matches_required:
        warnings.append("Auction governance does not match the configured Yearn trade handler.")
    warnings.extend(source.warnings)
    warnings.extend(discovery.notes)
    status = "ok"
    tx_hash = None
    tx_gas_estimate = None
    tx_broadcast_at = None

    if not eligible:
        status = "noop"
    elif broadcast:
        prompt = (
            "Preview failed. Broadcast enable-tokens transaction anyway?"
            if preview is not None and not preview.call_succeeded
            else "Broadcast enable-tokens transaction?"
        )
        if bypass_confirmation or typer.confirm(prompt, default=False):
            if exec_ctx.signer is None:
                raise SystemExit("Signer is required for broadcast execution.")
            tx_hash, tx_gas_estimate = enabler.send_execute_transaction(
                signer=exec_ctx.signer,
                trade_handler_address=inspection.governance,
                commands=commands,
                state=state,
            )
            tx_broadcast_at = utcnow_iso()
        else:
            status = "noop"
    elif preview is not None and not preview.call_succeeded:
        status = "error"

    data = {
        "inspection": asdict(inspection),
        "source": asdict(source),
        "discovery_notes": discovery.notes,
        "probes": [asdict(probe) for probe in probes],
        "selected_tokens": selected_addresses,
        "preview": asdict(preview) if preview is not None else None,
        "preview_sender": preview_sender,
        "preview_sender_authorized": preview_authorized,
        "commands_count": len(commands),
        "state_slots": len(state),
        "tx_hash": tx_hash,
        "tx_gas_estimate": tx_gas_estimate,
        "tx_sender": preview_sender,
        "tx_broadcast_at": tx_broadcast_at,
    }
    if json_output:
        emit_json("auction.enable-tokens", status=status, data=data, warnings=warnings)
    else:
        _print_auction_summary(inspection)
        typer.echo("Resolved source:")
        typer.echo(f"  type          {source.source_type}")
        typer.echo(f"  address       {to_checksum_address(source.source_address)}")
        typer.echo(f"  name          {source.source_name or 'unknown'}")
        typer.echo()
        for warning in warnings:
            typer.echo(f"Warning: {warning}")
        if warnings:
            typer.echo()
        typer.echo(f"Discovered {len(discovery.tokens_by_address)} unique token candidate(s).")
        _print_probe_table(probes)
        typer.echo("Wei-roll plan:")
        typer.echo(f"  enable calls  {len(selected_addresses)}")
        typer.echo(f"  commands      {len(commands)}")
        typer.echo(f"  state slots   {len(state)}")
        typer.echo()
        if preview is not None:
            if preview_sender:
                typer.echo(
                    "Preview sender mech authorization: "
                    f"{'yes' if preview_authorized else 'no'} ({to_checksum_address(preview_sender)})"
                )
            else:
                typer.echo("Preview sender mech authorization: skipped")
            typer.echo("Preview:")
            typer.echo(f"  execute call  {'ok' if preview.call_succeeded else 'failed'}")
            typer.echo(f"  gas estimate  {preview.gas_estimate if preview.gas_estimate is not None else 'unavailable'}")
            if preview.error_message:
                typer.echo(f"  detail        {preview.error_message}")
            typer.echo()
        if status == "noop" and not broadcast:
            typer.echo("No enable() calls need to be queued.")
        elif not broadcast:
            typer.echo("Dry run only. No transaction was sent.")
        elif status == "noop":
            typer.echo("Aborted before broadcast.")
        elif tx_hash is not None:
            typer.echo()
            render_broadcast_records(
                [
                    BroadcastRecord(
                        operation="enable-tokens",
                        sender=preview_sender,
                        tx_hash=tx_hash,
                        broadcast_at=tx_broadcast_at,
                        gas_estimate=tx_gas_estimate,
                    )
                ]
            )

    if status == "error":
        raise typer.Exit(code=EXECUTION_ERROR)
    if status == "noop":
        raise typer.Exit(code=NOOP)


@app.command("settle")
def settle(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction contract address."),
    config: ConfigOption = None,
    broadcast: BroadcastOption = False,
    bypass_confirmation: BypassConfirmationOption = False,
    token_address: str | None = typer.Option(None, "--token", help="Expected active token address."),
    sweep: bool = typer.Option(
        False,
        "--sweep",
        help="Force sweep-and-settle for the active lot, even if it is still above floor.",
    ),
    sender: SenderOption = None,
    account: AccountOption = None,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
    receipt_timeout: int = typer.Option(120, "--receipt-timeout", min=1, help="Seconds to wait for a receipt after broadcasting."),
) -> None:
    """Resolve the current active lot if it is settleable."""

    configure_logging(output_mode=OutputMode.TEXT)
    if bypass_confirmation and not broadcast:
        raise typer.BadParameter("--bypass-confirmation requires --broadcast", param_hint="--bypass-confirmation")
    cli_ctx = CLIContext(config, mode="server")
    normalized_auction_address = normalize_cli_address(auction_address, param_hint="AUCTION")
    normalized_token_address = normalize_cli_address(token_address, param_hint="--token")
    normalized_sender = normalize_cli_address(sender, param_hint="--sender")
    try:
        cli_ctx.require_rpc()
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    exec_ctx = cli_ctx.resolve_execution(
        broadcast=broadcast,
        required_for="broadcast settlement execution",
        sender=normalized_sender,
        account_name=account,
        keystore_path=keystore,
        password_file=password_file,
    )
    preview_sender = exec_ctx.sender
    inspection = asyncio.run(
        inspect_auction_settlement(
            build_web3_client(cli_ctx.settings),
            cli_ctx.settings,
            normalized_auction_address,
        )
    )
    decision = decide_auction_settlement(
        inspection,
        token_address=normalized_token_address,
        method="sweep_and_settle" if sweep else "auto",
        allow_above_floor=sweep,
    )

    warnings: list[str] = []
    if (
        sweep
        and decision.operation_type == "sweep_and_settle"
        and inspection.active_available_raw
        and inspection.active_price_public_raw is not None
        and inspection.minimum_price_public_raw is not None
        and inspection.active_price_public_raw > inspection.minimum_price_public_raw
    ):
        warnings.append(
            "Forced sweep requested while auction is still above floor; unsold tokens will be returned to the receiver."
        )
    execution = None
    if decision.status == "actionable":
        execution = asyncio.run(
            _preview_settlement_execution(
                settings=cli_ctx.settings,
                auction_address=normalized_auction_address,
                decision=decision,
                sender_address=preview_sender,
                broadcast=False,
                signer=exec_ctx.signer,
                receipt_timeout=receipt_timeout,
            )
        )
        if execution.get("warning"):
            warnings.append(str(execution["warning"]))

    status = "ok"
    if decision.status == "noop":
        status = "noop"
    elif decision.status == "error":
        status = "error"

    if broadcast and decision.status == "actionable":
        action_label = format_operation_type(decision.operation_type)
        prompt = (
            f"Preview failed. Broadcast {action_label} transaction anyway?"
            if warnings
            else f"Broadcast {action_label} transaction?"
        )
        if bypass_confirmation or typer.confirm(prompt, default=False):
            execution = asyncio.run(
                _preview_settlement_execution(
                    settings=cli_ctx.settings,
                    auction_address=normalized_auction_address,
                    decision=decision,
                    sender_address=preview_sender,
                    broadcast=True,
                    signer=exec_ctx.signer,
                    receipt_timeout=receipt_timeout,
                )
            )
            status = "ok" if execution.get("receipt_status") == "CONFIRMED" else "error"
            warnings = [str(execution["warning"])] if execution.get("warning") else warnings
        else:
            status = "noop"

    data = {
        "inspection": asdict(inspection),
        "decision": asdict(decision),
        "execution": execution,
    }
    if json_output:
        emit_json("auction.settle", status=status, data=data, warnings=warnings)
    else:
        _render_settlement_summary(
            inspection=inspection,
            decision=decision,
            execution=execution,
            warnings=warnings,
            broadcast=broadcast,
            status=status,
        )

    if status == "error":
        raise typer.Exit(code=EXECUTION_ERROR)
    if status == "noop":
        raise typer.Exit(code=NOOP)
