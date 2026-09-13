"""Local auction CLI contracts; transaction safety is exercised with a real ledger."""
from types import SimpleNamespace
import json

import pytest
import yaml
from typer.testing import CliRunner

from tidal.cli import app
from tidal.config import load_server_settings
from tidal.migrations import run_migrations
from tidal.resources import read_template_text
import tidal.auction_cli as auction_cli


AUCTION = "0x" + "2" * 40
TOKEN = "0x" + "3" * 40


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    values = yaml.safe_load(read_template_text("server.yaml"))
    values.update(db_path=str(tmp_path / "tidal.db"), rpc_url="https://example.invalid")
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump(values))
    settings = load_server_settings(config)
    run_migrations(settings.database_url)
    state = SimpleNamespace(config=config, calls=[], unlocks=[], outcome={
        "preparation_status": "ok", "transactions": [{"status": "CONFIRMED"}],
        "unsubmitted": 0, "warnings": [], "preview": {},
    })
    def unlock(*args, **kwargs):
        state.unlocks.append(kwargs)
        return SimpleNamespace(signer=object())
    async def execute(**kwargs):
        state.calls.append(kwargs)
        return state.outcome
    monkeypatch.setattr(auction_cli.CLIContext, "resolve_execution", unlock)
    assert not hasattr(auction_cli.CLIContext, "control_plane_client")
    monkeypatch.setattr(auction_cli, "run_auction_action", execute)
    return state


def invoke(native, action="enable-tokens", *args):
    return CliRunner().invoke(app, ["auction", action, AUCTION, "--config", str(native.config), *args])


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("statuses,code,exit_code", [
    (["CONFIRMED"], "OK", 0), (["REVERTED"], "EXECUTION_ERROR", 1),
    (["RECORDED"], "WAITING", 75), (["INCLUDED"], "WAITING", 75),
    (["CONFIRMED", "PENDING"], "WAITING", 75),
])
def test_text_and_json_report_retained_outcomes(native, json_output, statuses, code, exit_code):
    native.outcome["transactions"] = [{"status": item} for item in statuses]
    native.outcome["unsubmitted"] = 1
    response = invoke(native, "enable-tokens", "--no-confirmation", *(["--json"] if json_output else []))
    assert response.exit_code == exit_code, response.output
    if json_output:
        payload = json.loads(response.stdout)
        assert payload["interface_version"] == 1 and payload["code"] == code
        assert payload["data"]["unsubmitted"] == 1
    assert len(native.calls) == len(native.unlocks) == 1


def test_extra_tokens_and_explicit_credentials_reach_local_owner(native):
    response = invoke(native, "enable-tokens", "--no-confirmation", "--json",
                      "--extra-token", TOKEN, "--extra-token", AUCTION)
    assert response.exit_code == 0, response.output
    assert native.calls[0]["extra_tokens"] == [TOKEN, AUCTION]
    assert native.calls[0]["confirm"] is None


def test_force_requires_exact_token_before_unlock(native):
    response = invoke(native, "settle", "--force", "--no-confirmation")
    assert response.exit_code == 2 and native.unlocks == []
    response = invoke(native, "settle", "--force", "--token", TOKEN, "--no-confirmation", "--json")
    assert response.exit_code == 0, response.output
    assert native.calls[0]["force"] and native.calls[0]["token"] == TOKEN


def test_sweep_requires_token_and_uses_local_sender(native):
    response = invoke(native, "sweep", "--token", TOKEN, "--no-confirmation", "--json")
    assert response.exit_code == 0, response.output
    assert native.calls[0]["action"] == "sweep"
    assert native.calls[0]["token"] == TOKEN


def test_json_requires_explicit_consent_before_unlock(native):
    response = invoke(native, "enable-tokens", "--json")
    assert response.exit_code == 2 and native.unlocks == []


def test_no_work_is_success_and_partial_failure_keeps_identity_in_output(native):
    native.outcome.update(preparation_status="noop", transactions=[])
    assert invoke(native, "settle", "--no-confirmation", "--json").exit_code == 0
    native.outcome.update(preparation_status="ok", transactions=[{"status": "CONFIRMED", "tx_hash": "retained"}],
                          blockers=[{"code": "STALE_PREPARATION", "message": "Prepare the remaining work again."}])
    response = invoke(native, "settle", "--no-confirmation", "--json")
    assert response.exit_code == 1, response.output
    assert json.loads(response.stdout)["data"]["transactions"][0]["tx_hash"] == "retained"


def test_managed_deployment_and_remote_transport_are_not_exposed():
    response = CliRunner().invoke(app, ["auction", "--help"])
    assert response.exit_code == 0 and "deploy" not in response.output
    response = CliRunner().invoke(app, ["auction", "enable-tokens", "--help"])
    assert "--api-key" not in response.output and "--api-base-url" not in response.output
    assert "repeat" in response.output
