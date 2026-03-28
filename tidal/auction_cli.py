"""Auction operator CLI commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from eth_utils import to_checksum_address

from tidal.chain.contracts.abis import AUCTION_KICKER_ABI
from tidal.cli_support import (
    build_sync_web3,
    discover_local_keystore_path,
    maybe_load_signer,
    prompt_bool,
    prompt_optional_address,
    prompt_text,
    read_keystore_address,
)
from tidal.config import load_settings
from tidal.errors import AddressNormalizationError
from tidal.logging import configure_logging
from tidal.normalizers import normalize_address
from tidal.ops.auction_enable import (
    AuctionInspection,
    AuctionTokenEnabler,
    TokenProbe,
    format_probe_reason,
    parse_manual_token_input,
)
from tidal.runtime import build_web3_client
from tidal.transaction_service.kicker import _DEFAULT_PRIORITY_FEE_GWEI, _format_execution_error
from tidal.transaction_service.signer import TransactionSigner

app = typer.Typer(help="Auction operator commands")


def _normalize_address_value(value: str, *, param_hint: str) -> str:
    try:
        return normalize_address(value.strip())
    except AddressNormalizationError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc


def _normalize_address_list(values: list[str] | None, *, param_hint: str) -> list[str]:
    return [_normalize_address_value(value, param_hint=param_hint) for value in values or []]


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


def _prompt_token_selection(eligible: list[TokenProbe]) -> list[TokenProbe]:
    if not eligible:
        return []

    typer.echo("Eligible tokens:")
    for index, probe in enumerate(eligible, 1):
        balance = probe.normalized_balance or "?"
        typer.echo(f"  [{index:02d}] {probe.display_label} | balance={balance} | origins={','.join(probe.origins)}")
    typer.echo()

    while True:
        raw = prompt_text(
            "Skip any eligible token numbers (comma-separated, blank keeps all)",
            required=False,
        )
        if not raw:
            return eligible

        try:
            skip_indexes = {
                int(chunk.strip(), 10)
                for chunk in raw.split(",")
                if chunk.strip()
            }
        except ValueError:
            typer.echo("Enter token numbers like 1,3 or leave blank.")
            continue

        if any(index < 1 or index > len(eligible) for index in skip_indexes):
            typer.echo("One or more token numbers are out of range.")
            continue

        return [
            probe
            for index, probe in enumerate(eligible, 1)
            if index not in skip_indexes
        ]


def _prompt_manual_tokens() -> list[str]:
    while True:
        raw = prompt_text(
            "Additional token addresses to probe (comma-separated, blank for none)",
            required=False,
        )
        if not raw:
            return []
        try:
            return parse_manual_token_input(raw)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"Invalid token list: {exc}")


async def _run_sweep_and_settle(
    *,
    auction_address: str,
    token_address: str,
    config: Path | None,
    broadcast: bool,
    receipt_timeout: int,
) -> int:
    settings = load_settings(config)
    if not settings.rpc_url:
        raise SystemExit("RPC_URL is required")
    if not settings.txn_keystore_path or not settings.txn_keystore_passphrase:
        raise SystemExit("TXN_KEYSTORE_PATH and TXN_KEYSTORE_PASSPHRASE must be configured")

    signer = TransactionSigner(settings.txn_keystore_path, settings.txn_keystore_passphrase)
    web3_client = build_web3_client(settings)

    auction = to_checksum_address(auction_address)
    token = to_checksum_address(token_address)
    kicker_address = to_checksum_address(settings.auction_kicker_address)
    contract = web3_client.contract(kicker_address, AUCTION_KICKER_ABI)
    tx_data = contract.functions.sweepAndSettle(auction, token)._encode_transaction_data()

    try:
        gas_estimate = await web3_client.estimate_gas(
            {
                "from": signer.checksum_address,
                "to": kicker_address,
                "data": tx_data,
                "chainId": settings.chain_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Gas estimate failed: {_format_execution_error(exc)}") from exc

    base_fee_wei = await web3_client.get_base_fee()
    base_fee_gwei = base_fee_wei / 1e9
    try:
        suggested_priority_fee_wei = await web3_client.get_max_priority_fee()
    except Exception:  # noqa: BLE001
        suggested_priority_fee_wei = int(_DEFAULT_PRIORITY_FEE_GWEI * 1e9)
    priority_fee_wei = min(suggested_priority_fee_wei, settings.txn_max_priority_fee_gwei * 10**9)

    gas_limit = min(int(gas_estimate * 1.2), settings.txn_max_gas_limit)
    tx = {
        "to": kicker_address,
        "data": tx_data,
        "chainId": settings.chain_id,
        "gas": gas_limit,
        "maxFeePerGas": int((max(settings.txn_max_base_fee_gwei, base_fee_gwei) + settings.txn_max_priority_fee_gwei) * 10**9),
        "maxPriorityFeePerGas": priority_fee_wei,
        "nonce": await web3_client.get_transaction_count(signer.address),
        "type": 2,
    }

    typer.echo(f"AuctionKicker: {kicker_address}")
    typer.echo(f"Auction:       {auction}")
    typer.echo(f"Sell token:    {token}")
    typer.echo(f"From:          {signer.checksum_address}")
    typer.echo(f"Gas estimate:  {gas_estimate}")
    typer.echo(f"Gas limit:     {gas_limit}")
    typer.echo(f"Base fee:      {base_fee_gwei:.4f} gwei")
    typer.echo(f"Priority fee:  {priority_fee_wei / 1e9:.4f} gwei")
    typer.echo(f"Data:          {tx_data}")

    if not broadcast:
        typer.echo("Dry run only. Re-run with --broadcast to send.")
        return 0

    try:
        signed_tx = signer.sign_transaction(tx)
        tx_hash = await web3_client.send_raw_transaction(signed_tx)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Transaction submission failed: {exc}") from exc
    typer.echo(f"Submitted:     {tx_hash}")

    try:
        receipt = await web3_client.get_transaction_receipt(tx_hash, timeout_seconds=receipt_timeout)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Receipt lookup failed: {exc}") from exc

    status = "CONFIRMED" if receipt.get("status") == 1 else "REVERTED"
    typer.echo(f"Receipt:       {status}")
    typer.echo(f"Block:         {receipt.get('blockNumber')}")
    typer.echo(f"Gas used:      {receipt.get('gasUsed')}")
    return 0 if status == "CONFIRMED" else 1


@app.command("enable-tokens")
def enable_tokens(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction address to inspect"),
    config: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    extra_token: list[str] | None = typer.Option(
        None,
        "--extra-token",
        help="Extra token address to probe. Can be supplied multiple times.",
    ),
) -> None:
    """Inspect an auction and queue enable(address) calls for relevant tokens."""

    configure_logging()
    normalized_auction_address = _normalize_address_value(auction_address, param_hint="AUCTION")
    normalized_extra_tokens = _normalize_address_list(extra_token, param_hint="--extra-token")

    settings = load_settings(config)
    w3 = build_sync_web3(settings)
    enabler = AuctionTokenEnabler(w3, settings)

    try:
        inspection = enabler.inspect_auction(normalized_auction_address)
        _print_auction_summary(inspection)

        if not inspection.in_configured_factory:
            if not prompt_bool("Auction is not in the configured factory. Continue anyway?", default=False):
                typer.echo("Aborted.")
                raise typer.Exit()

        if not inspection.governance_matches_required:
            if not prompt_bool(
                "Auction governance does not match the configured Yearn trade handler. Continue anyway?",
                default=False,
            ):
                typer.echo("Aborted.")
                raise typer.Exit()

        source = enabler.resolve_source(inspection)
        typer.echo("Resolved source:")
        typer.echo(f"  type          {source.source_type}")
        typer.echo(f"  address       {to_checksum_address(source.source_address)}")
        typer.echo(f"  name          {source.source_name or 'unknown'}")
        typer.echo()

        for warning in source.warnings:
            typer.echo(f"Warning: {warning}")
        if source.warnings:
            typer.echo()

        manual_tokens = list(normalized_extra_tokens)
        manual_tokens.extend(_prompt_manual_tokens())

        discovery = enabler.discover_tokens(
            inspection=inspection,
            source=source,
            manual_tokens=manual_tokens,
        )
        typer.echo(f"Discovered {len(discovery.tokens_by_address)} unique token candidate(s).")
        for note in discovery.notes:
            typer.echo(f"Note: {note}")
        if discovery.notes:
            typer.echo()

        probes = enabler.probe_tokens(
            inspection=inspection,
            source=source,
            discovery=discovery,
        )
        _print_probe_table(probes)

        eligible = [probe for probe in probes if probe.status == "eligible"]
        if not eligible:
            typer.echo("No enable() calls need to be queued.")
            return

        selected = _prompt_token_selection(eligible)
        if not selected:
            typer.echo("All eligible tokens were removed from the plan.")
            return

        selected_addresses = [probe.token_address for probe in selected]
        commands, state = enabler.build_enable_plan(
            inspection=inspection,
            tokens=selected_addresses,
        )

        default_keystore_path = discover_local_keystore_path(settings)
        default_preview_caller = read_keystore_address(default_keystore_path)

        typer.echo("Wei-roll plan:")
        typer.echo(f"  enable calls  {len(selected_addresses)}")
        typer.echo(f"  commands      {len(commands)}")
        typer.echo(f"  state slots   {len(state)}")
        typer.echo()

        if default_keystore_path is not None:
            typer.echo(f"Detected local keystore: {default_keystore_path}")
        use_live = prompt_bool(
            "Broadcast a live trade handler transaction?",
            default=default_keystore_path is not None,
        )
        signer = maybe_load_signer(settings, required=use_live, required_for="live enable-tokens execution")

        if signer is not None:
            preview_caller = signer.address
        else:
            preview_caller = prompt_optional_address(
                "Caller address for execute() preview",
                default=default_preview_caller,
            )

        if preview_caller:
            is_mech = enabler.is_authorized_mech(inspection.governance, preview_caller)
            typer.echo(
                "Preview caller mech authorization: "
                f"{'yes' if is_mech else 'no'} ({to_checksum_address(preview_caller)})"
            )
        else:
            typer.echo("Preview caller mech authorization: skipped")

        preview = enabler.preview_execution(
            trade_handler_address=inspection.governance,
            commands=commands,
            state=state,
            caller_address=preview_caller,
        )
        typer.echo("Preview:")
        typer.echo(f"  execute call  {'ok' if preview.call_succeeded else 'failed'}")
        typer.echo(f"  gas estimate  {preview.gas_estimate if preview.gas_estimate is not None else 'unavailable'}")
        if preview.error_message:
            typer.echo(f"  detail        {preview.error_message}")
        typer.echo()

        if not use_live:
            typer.echo("Dry run only. No transaction was sent.")
            return

        if signer is None:
            raise SystemExit("Signer is required for live execution.")

        if not preview.call_succeeded:
            if not prompt_bool("Preview failed. Send the transaction anyway?", default=False):
                typer.echo("Aborted before broadcast.")
                return

        if not prompt_bool("Send transaction now?", default=False):
            typer.echo("Aborted before broadcast.")
            return

        tx_hash, gas_estimate = enabler.send_execute_transaction(
            signer=signer,
            trade_handler_address=inspection.governance,
            commands=commands,
            state=state,
        )
        typer.echo("Transaction sent:")
        typer.echo(f"  tx hash       {tx_hash}")
        typer.echo(f"  gas estimate  {gas_estimate}")
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command("sweep-and-settle")
def sweep_and_settle(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction contract address"),
    token_address: str = typer.Argument(..., metavar="TOKEN", help="Sell token to sweep and settle"),
    config: Path | None = typer.Option(None, exists=True, file_okay=True, dir_okay=False),
    broadcast: bool = typer.Option(
        False,
        help="Sign and send the transaction. Without this flag the command only prints the prepared transaction.",
    ),
    receipt_timeout: int = typer.Option(
        120,
        min=1,
        help="Seconds to wait for a receipt after broadcasting",
    ),
) -> None:
    """Call AuctionKicker.sweepAndSettle() for a specific auction token."""

    configure_logging()
    normalized_auction_address = _normalize_address_value(auction_address, param_hint="AUCTION")
    normalized_token_address = _normalize_address_value(token_address, param_hint="TOKEN")
    exit_code = asyncio.run(
        _run_sweep_and_settle(
            auction_address=normalized_auction_address,
            token_address=normalized_token_address,
            config=config,
            broadcast=broadcast,
            receipt_timeout=receipt_timeout,
        )
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
