"""API-backed auction operator commands."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict

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
from tidal.cli_renderers import emit_json, format_settlement_reason_lines, render_execution_result, render_status_panel
from tidal.cli_exit_codes import EXECUTION_ERROR, NOOP, VALIDATION_ERROR
from tidal.control_plane.client import ControlPlaneError
from tidal.errors import ConfigurationError
from tidal.execution_result import summarize_execution
from tidal.operator_cli_support import (
    execute_prepared_action_sync,
    progress_status,
    render_action_preview,
    render_broadcast_result,
    submission_progress,
    render_warnings,
    validate_prepared_gas_limits,
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

    if command_name not in {"auction.settle", "auction.sweep"}:
        return lines

    prepared_operations = preview.get("preparedOperations")
    if isinstance(prepared_operations, list) and prepared_operations:
        lines.append("")
        lines.append("Prepared operations")
        for operation in prepared_operations:
            if not isinstance(operation, dict):
                continue
            token = operation.get("tokenAddress")
            reason = operation.get("reason")
            if token is not None:
                lines.append(f"  Token:        {normalize_cli_address(str(token), param_hint='token')}")
            if reason:
                lines.extend(format_settlement_reason_lines(str(reason), prefix="  Reason:      "))
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
    gas_limit_error: str | None = None
    if response["status"] == "ok":
        try:
            validate_prepared_gas_limits(tx_intents)
        except RuntimeError as exc:
            gas_limit_error = str(exc)
    if not json_output:
        if response["status"] == "ok" and isinstance(transactions, list) and transactions:
            render_action_preview(data, heading="Prepared action")
        render_warnings(list(response.get("warnings") or []))
        if gas_limit_error:
            render_status_panel(
                "Preparation Failed",
                [
                    "Prepared transaction gas limit is below its estimate.",
                    gas_limit_error,
                ],
                border_style="red",
            )
    if gas_limit_error:
        if json_output:
            output = dict(data)
            output["broadcastRecords"] = broadcast_records
            emit_json(command_name, status="error", data=output, warnings=[gas_limit_error])
        raise typer.Exit(code=VALIDATION_ERROR)
    try:
        with cli_ctx.control_plane_client() as client:
            if response["status"] == "ok":
                tx_count = len(transactions)
                confirmation_prompt = "Send this transaction?" if tx_count == 1 else f"Send {tx_count} transaction(s)?"
                if not no_confirmation and not typer.confirm(confirmation_prompt, default=False):
                    raise typer.Exit(code=NOOP)
                if exec_ctx.signer is None or exec_ctx.sender is None:
                    raise typer.Exit(code=VALIDATION_ERROR)
                with (nullcontext(None) if json_output else submission_progress("Submitting transaction...")) as update_progress:
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
        if isinstance(exc, typer.Exit):
            raise
        if json_output:
            emit_json(command_name, status="failed", data={**data, "broadcastRecords": broadcast_records}, warnings=[str(exc)])
        else:
            render_status_panel("Execution Failed", [str(exc)], border_style="red")
        raise typer.Exit(code=EXECUTION_ERROR) from exc

    result = summarize_execution(broadcast_records, expected_count=len(tx_intents))
    if json_output:
        output = dict(data)
        output["broadcastRecords"] = broadcast_records
        output["execution"] = asdict(result)
        status = result.status if response["status"] == "ok" else response["status"]
        emit_json(command_name, status=status, data=output, warnings=response.get("warnings"))
        raise typer.Exit(code=EXECUTION_ERROR if response["status"] == "error" else result.exit_code)

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
        render_execution_result(result)

    if response["status"] == "noop":
        raise typer.Exit(code=NOOP)
    if response["status"] == "error":
        raise typer.Exit(code=EXECUTION_ERROR)
    raise typer.Exit(code=result.exit_code)


@app.command("deploy")
def deploy(
    want: str = typer.Option(..., "--want", help="Want token address."),
    receiver: str = typer.Option(..., "--receiver", help="Auction receiver address."),
    starting_price: int = typer.Option(
        ...,
        "--starting-price",
        min=1,
        help="Contract-raw startingPrice; units are determined by the selected factory version.",
    ),
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
    auction_address: str = typer.Argument(
        ...,
        metavar="AUCTION",
        help="Auction address to inspect. Configured fee-burner address/want aliases are accepted.",
    ),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    extra_token: list[str] | None = typer.Option(
        None,
        "--extra-token",
        help="Include a custom token address in enable discovery. Repeat to add more than one.",
    ),
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
        "txnMaxGasLimit": cli_ctx.settings.txn_max_gas_limit,
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
    token_address: str | None = typer.Option(None, "--token", help="Restrict settlement to a specific sell token."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow resolving a live lot that still has sell balance.",
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
    if force and not token_address:
        raise typer.BadParameter("--force requires --token")
    payload = {
        "sender": exec_ctx.sender,
        "tokenAddress": normalize_cli_address(token_address, param_hint="--token") if token_address else None,
        "force": force,
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


@app.command("sweep")
def sweep(
    auction_address: str = typer.Argument(..., metavar="AUCTION", help="Auction contract address."),
    config: ConfigOption = None,
    api_base_url: ApiBaseUrlOption = None,
    api_key: ApiKeyOption = None,
    no_confirmation: NoConfirmationOption = False,
    token_address: str = typer.Option(..., "--token", help="Sell token to sweep from the auction."),
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
        required_for="auction sweep execution",
        keystore_path=keystore,
        password_file=password_file,
    )
    payload = {
        "sender": exec_ctx.sender,
        "tokenAddress": normalize_cli_address(token_address, param_hint="--token"),
    }
    try:
        with cli_ctx.control_plane_client() as client:
            if json_output:
                response = client.prepare_sweep(
                    normalize_cli_address(auction_address, param_hint="AUCTION"),
                    payload,
                )
            else:
                with progress_status("Preparing manual sweep..."):
                    response = client.prepare_sweep(
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
        command_name="auction.sweep",
    )
