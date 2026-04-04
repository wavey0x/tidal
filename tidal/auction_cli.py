"""API-backed auction operator commands."""

from __future__ import annotations

import typer

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import (
    ApiBaseUrlOption,
    ApiKeyOption,
    ConfigOption,
    JsonOption,
    KeystoreOption,
    NoConfirmationOption,
    PasswordFileOption,
)
from tidal.cli_validation import require_no_confirmation_for_json
from tidal.cli_renderers import emit_json, format_settlement_reason_lines, render_status_panel
from tidal.control_plane.client import ControlPlaneError
from tidal.errors import ConfigurationError
from tidal.operator_cli_support import (
    execute_prepared_action_sync,
    progress_status,
    render_action_preview,
    render_broadcast_result,
    submission_progress,
    render_warnings,
)
from tidal.transaction_service.types import TxIntent

app = typer.Typer(help="Auction operator commands", no_args_is_help=True)


def _noop_status_lines(*, command_name: str, data: dict[str, object]) -> list[str]:
    lines = ["No transaction was prepared."]
    preview = data.get("preview")
    if not isinstance(preview, dict):
        return lines

    decision = preview.get("decision")
    if isinstance(decision, dict):
        reason = str(decision.get("reason") or "").strip()
        if reason:
            lines.extend(format_settlement_reason_lines(reason))

    if command_name != "auction.settle":
        return lines

    inspection = preview.get("inspection")
    if not isinstance(inspection, dict):
        return lines

    active_token = inspection.get("active_token")
    active_available_raw = inspection.get("active_available_raw")
    active_price_public_raw = inspection.get("active_price_public_raw")
    minimum_price_public_raw = inspection.get("minimum_price_public_raw")
    minimum_price_scaled_1e18 = inspection.get("minimum_price_scaled_1e18")
    inactive_token = inspection.get("inactive_token")
    inactive_token_balance_raw = inspection.get("inactive_token_balance_raw")
    inactive_token_kickable_raw = inspection.get("inactive_token_kickable_raw")
    inactive_token_kicked_at = inspection.get("inactive_token_kicked_at")
    auction_length_seconds = inspection.get("auction_length_seconds")
    if (
        active_token is None
        and active_available_raw is None
        and active_price_public_raw is None
        and minimum_price_public_raw is None
        and minimum_price_scaled_1e18 is None
        and inactive_token is None
        and inactive_token_balance_raw is None
        and inactive_token_kickable_raw is None
        and inactive_token_kicked_at is None
        and auction_length_seconds is None
    ):
        return lines

    lines.append("")
    lines.append("Settlement state")
    if active_token is not None:
        lines.append(f"  Active token:  {normalize_cli_address(str(active_token), param_hint='token')}")
    if active_available_raw is not None:
        lines.append(f"  Available:     {active_available_raw}")
    if active_price_public_raw is not None:
        lines.append(f"  Live price:    {active_price_public_raw}")
    if minimum_price_public_raw is not None:
        lines.append(f"  Floor price:   {minimum_price_public_raw}")
    if minimum_price_scaled_1e18 is not None:
        lines.append(f"  Min price:     {minimum_price_scaled_1e18} (scaled 1e18)")
    if inactive_token is not None:
        lines.append(f"  Inactive token:{normalize_cli_address(str(inactive_token), param_hint='token')}")
    if inactive_token_balance_raw is not None:
        lines.append(f"  Auction bal:   {inactive_token_balance_raw}")
    if inactive_token_kickable_raw is not None:
        lines.append(f"  Kickable:      {inactive_token_kickable_raw}")
    if inactive_token_kicked_at is not None:
        lines.append(f"  Kicked at:     {inactive_token_kicked_at}")
    if auction_length_seconds is not None:
        lines.append(f"  Auction len:   {auction_length_seconds}s")
    return lines


def _handle_prepared_action(
    *,
    cli_ctx: CLIContext,
    response: dict[str, object],
    data: dict[str, object],
    no_confirmation: bool,
    exec_ctx,
    json_output: bool,
    command_name: str,
) -> None:  # noqa: ANN001
    broadcast_records: list[dict[str, object]] = []
    transactions = data.get("transactions") or []
    tx_intents = [TxIntent.from_payload(tx) for tx in transactions] if isinstance(transactions, list) else []
    if not json_output:
        if response["status"] == "ok" and isinstance(transactions, list) and transactions:
            render_action_preview(data, heading="Prepared action")
        render_warnings(list(response.get("warnings") or []))
    try:
        with cli_ctx.control_plane_client() as client:
            if response["status"] == "ok":
                tx_count = len(transactions)
                confirmation_prompt = "Send this transaction?" if tx_count == 1 else f"Send {tx_count} transaction(s)?"
                if not no_confirmation and not typer.confirm(confirmation_prompt, default=False):
                    raise typer.Exit(code=2)
                if exec_ctx.signer is None or exec_ctx.sender is None:
                    raise typer.Exit(code=1)
                with submission_progress("Submitting transaction...") as update_progress:
                    broadcast_records = execute_prepared_action_sync(
                        settings=cli_ctx.settings,
                        client=client,
                        action_id=str(data["actionId"]),
                        sender=exec_ctx.sender,
                        signer=exec_ctx.signer,
                        transactions=tx_intents,
                        progress_callback=update_progress,
                    )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        output = dict(data)
        output["broadcastRecords"] = broadcast_records
        emit_json(command_name, status=response["status"], data=output, warnings=response.get("warnings"))
        return

    if response["status"] == "noop":
        render_status_panel(
            "No Transaction Prepared",
            _noop_status_lines(command_name=command_name, data=data),
            border_style="yellow",
        )
    elif response["status"] == "error":
        render_status_panel(
            "Preparation Failed",
            ["No transaction was prepared."],
            border_style="red",
        )
    else:
        render_broadcast_result(broadcast_records)

    if response["status"] == "noop":
        raise typer.Exit(code=2)
    if response["status"] == "error":
        raise typer.Exit(code=1)


@app.command("deploy")
def deploy(
    want: str = typer.Option(..., "--want", help="Want token address."),
    receiver: str = typer.Option(..., "--receiver", help="Auction receiver address."),
    starting_price: int = typer.Option(..., "--starting-price", min=0, help="Starting price for the new auction."),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    factory: str | None = typer.Option(None, "--factory", help="Auction factory address."),
    governance: str | None = typer.Option(None, "--governance", help="Governance / trade handler address."),
    salt: str | None = typer.Option(None, "--salt", help="Optional deployment salt."),
    no_confirmation: NoConfirmationOption = False,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
) -> None:
    require_no_confirmation_for_json(json_output=json_output, no_confirmation=no_confirmation)
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    try:
        cli_ctx.verify_authenticated_api_access()
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    exec_ctx = cli_ctx.resolve_execution(
        required=True,
        required_for="auction deployment",
        keystore_path=keystore,
        password_file=password_file,
    )
    payload = {
        "want": normalize_cli_address(want, param_hint="--want"),
        "receiver": normalize_cli_address(receiver, param_hint="--receiver"),
        "sender": exec_ctx.sender,
        "factory": normalize_cli_address(factory, param_hint="--factory") if factory else None,
        "governance": normalize_cli_address(governance, param_hint="--governance") if governance else None,
        "startingPrice": starting_price,
        "salt": salt,
    }
    try:
        with cli_ctx.control_plane_client() as client:
            if json_output:
                response = client.prepare_deploy(payload)
            else:
                with progress_status("Preparing deployment..."):
                    response = client.prepare_deploy(payload)
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _handle_prepared_action(
        cli_ctx=cli_ctx,
        response=response,
        data=response["data"],
        no_confirmation=no_confirmation,
        exec_ctx=exec_ctx,
        json_output=json_output,
        command_name="auction.deploy",
    )


@app.command("enable-tokens")
def enable_tokens(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction address to inspect."),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    extra_token: list[str] | None = typer.Option(None, "--extra-token", help="Extra token address to probe."),
    no_confirmation: NoConfirmationOption = False,
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
) -> None:
    require_no_confirmation_for_json(json_output=json_output, no_confirmation=no_confirmation)
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    try:
        cli_ctx.verify_authenticated_api_access()
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    exec_ctx = cli_ctx.resolve_execution(
        required=True,
        required_for="enable-tokens execution",
        keystore_path=keystore,
        password_file=password_file,
    )
    payload = {
        "sender": exec_ctx.sender,
        "extraTokens": [normalize_cli_address(value, param_hint="--extra-token") for value in extra_token or []],
    }
    try:
        with cli_ctx.control_plane_client() as client:
            if json_output:
                response = client.prepare_enable_tokens(
                    normalize_cli_address(auction_address, param_hint="AUCTION"),
                    payload,
                )
            else:
                with progress_status("Preparing token enable..."):
                    response = client.prepare_enable_tokens(
                        normalize_cli_address(auction_address, param_hint="AUCTION"),
                        payload,
                    )
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _handle_prepared_action(
        cli_ctx=cli_ctx,
        response=response,
        data=response["data"],
        no_confirmation=no_confirmation,
        exec_ctx=exec_ctx,
        json_output=json_output,
        command_name="auction.enable-tokens",
    )


@app.command("settle")
def settle(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction contract address."),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    no_confirmation: NoConfirmationOption = False,
    token_address: str | None = typer.Option(None, "--token", help="Expected active token address."),
    sweep: bool = typer.Option(
        False,
        "--sweep",
        help="Force sweep-and-settle for the active lot, even if it is still above floor.",
    ),
    keystore: KeystoreOption = None,
    password_file: PasswordFileOption = None,
    json_output: JsonOption = False,
) -> None:
    require_no_confirmation_for_json(json_output=json_output, no_confirmation=no_confirmation)
    cli_ctx = CLIContext(config, api_base_url=api_base_url, api_key=api_key)
    try:
        cli_ctx.verify_authenticated_api_access()
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    exec_ctx = cli_ctx.resolve_execution(
        required=True,
        required_for="settlement execution",
        keystore_path=keystore,
        password_file=password_file,
    )
    payload = {
        "sender": exec_ctx.sender,
        "tokenAddress": normalize_cli_address(token_address, param_hint="--token") if token_address else None,
        "sweep": sweep,
    }
    try:
        with cli_ctx.control_plane_client() as client:
            if json_output:
                response = client.prepare_settle(
                    normalize_cli_address(auction_address, param_hint="AUCTION"),
                    payload,
                )
            else:
                with progress_status("Preparing settlement..."):
                    response = client.prepare_settle(
                        normalize_cli_address(auction_address, param_hint="AUCTION"),
                        payload,
                    )
    except (ConfigurationError, ControlPlaneError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _handle_prepared_action(
        cli_ctx=cli_ctx,
        response=response,
        data=response["data"],
        no_confirmation=no_confirmation,
        exec_ctx=exec_ctx,
        json_output=json_output,
        command_name="auction.settle",
    )
