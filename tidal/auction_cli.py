"""Local managed auction operations; browser wallets retain deployment."""
from __future__ import annotations

import asyncio
import typer

from tidal.cli_context import CLIContext, normalize_cli_address
from tidal.cli_options import ConfigOption, JsonOption, KeystoreOption, NoConfirmationOption, PasswordFileOption
from tidal.cli_renderers import render_prepared_action_summary
from tidal.cli_validation import require_no_confirmation_for_json
from tidal.lifecycle import result
from tidal.lifecycle_cli import emit_operation
from tidal.native_actions import run_auction_action

app = typer.Typer(help="Local auction operations", no_args_is_help=True)


def _run(*, action, auction, config, json_output, no_confirmation, keystore, password_file,
         token=None, extra_tokens=None, force=False):
    require_no_confirmation_for_json(json_output=json_output, no_confirmation=no_confirmation)
    ctx = CLIContext(config)
    auction = normalize_cli_address(auction, param_hint="AUCTION")
    token = normalize_cli_address(token, param_hint="--token")
    if force and token is None:
        raise typer.BadParameter("--force requires --token")
    tokens = [normalize_cli_address(item, param_hint="--extra-token") for item in extra_tokens or []]

    def confirm(prepared):
        render_prepared_action_summary(prepared)
        return typer.confirm("Submit the prepared transaction(s)?", default=False)

    async def execute():
        ctx.require_rpc()
        execution = ctx.resolve_execution(required=True, required_for=f"local auction {action}",
                                          keystore_path=keystore, password_file=password_file)
        with ctx.session() as session:
            outcome = await run_auction_action(
                settings=ctx.settings, session=session, signer=execution.signer, action=action,
                auction=auction, token=token, extra_tokens=tokens, force=force,
                confirm=None if no_confirmation else confirm,
            )
        attempts = outcome["transactions"]
        pending = any(item["status"] not in {"CONFIRMED", "REVERTED"} for item in attempts)
        failed = outcome["preparation_status"] == "error" or bool(outcome.get("blockers")) or any(item["status"] == "REVERTED" for item in attempts)
        code = "WAITING" if pending else "EXECUTION_ERROR" if failed else "OK"
        blockers = outcome.get("blockers", []) or ([{"code": code, "message": "Inspect retained transaction state before continuing."}] if pending or failed else [])
        return result(code, data=outcome, blockers=blockers,
                      warnings=[{"code": "PREPARATION_WARNING", "message": warning} for warning in outcome["warnings"]])

    emit_operation(lambda: asyncio.run(execute()), json_output=json_output)


@app.command("enable-tokens")
def enable_tokens(
    auction_address: str = typer.Argument(..., metavar="AUCTION"),
    config: ConfigOption = None, json_output: JsonOption = False,
    no_confirmation: NoConfirmationOption = False,
    extra_token: list[str] | None = typer.Option(None, "--extra-token", help="Additional token to probe; repeat for multiple tokens."),
    keystore: KeystoreOption = None, password_file: PasswordFileOption = None,
):
    _run(action="enable_tokens", auction=auction_address, config=config, json_output=json_output,
         no_confirmation=no_confirmation, keystore=keystore, password_file=password_file, extra_tokens=extra_token)


@app.command("settle")
def settle(
    auction_address: str = typer.Argument(..., metavar="AUCTION"),
    config: ConfigOption = None, json_output: JsonOption = False,
    no_confirmation: NoConfirmationOption = False,
    token_address: str | None = typer.Option(None, "--token"),
    force: bool = typer.Option(False, "--force", help="Allow resolving a live funded lot; requires --token."),
    keystore: KeystoreOption = None, password_file: PasswordFileOption = None,
):
    _run(action="settle", auction=auction_address, config=config, json_output=json_output,
         no_confirmation=no_confirmation, keystore=keystore, password_file=password_file, token=token_address, force=force)


@app.command("sweep")
def sweep(
    auction_address: str = typer.Argument(..., metavar="AUCTION"),
    token_address: str = typer.Option(..., "--token"),
    config: ConfigOption = None, json_output: JsonOption = False,
    no_confirmation: NoConfirmationOption = False,
    keystore: KeystoreOption = None, password_file: PasswordFileOption = None,
):
    _run(action="sweep", auction=auction_address, config=config, json_output=json_output,
         no_confirmation=no_confirmation, keystore=keystore, password_file=password_file, token=token_address)
