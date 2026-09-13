"""Native CLI contracts; sending and policy behavior are tested at their owners."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import pytest
import yaml
from typer.testing import CliRunner

from tidal.cli import app
from tidal.config import load_server_settings
from tidal.migrations import run_migrations
from tidal.resources import read_template_text
from tidal.transaction_service.types import TxnRunResult
import tidal.kick_cli as kick_cli


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("TIDAL_HOME", str(tmp_path))
    values = yaml.safe_load(read_template_text("server.yaml"))
    values.update(db_path=str(tmp_path / "tidal.db"), rpc_url="https://example.invalid")
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump(values))
    settings = load_server_settings(config)
    run_migrations(settings.database_url)
    captured = []
    unlocks = []

    def unlock(*args, **kwargs):
        unlocks.append(kwargs)
        return SimpleNamespace(signer=object(), sender="0x" + "1" * 40)

    def build(effective, session, **kwargs):
        async def run_once(**options):
            captured.append((effective, kwargs, options))
            return TxnRunResult(
                run_id="fixture-" + options["source_type"], status="DRY_RUN" if not options["live"] else "SUCCESS",
                candidates_found=0, kicks_attempted=0, kicks_succeeded=0, kicks_failed=0,
            )
        return SimpleNamespace(run_once=run_once)

    monkeypatch.setattr(kick_cli.CLIContext, "resolve_execution", unlock)
    assert not hasattr(kick_cli.CLIContext, "control_plane_client")
    monkeypatch.setattr(kick_cli, "build_txn_service", build)
    return SimpleNamespace(config=config, captured=captured, unlocks=unlocks, settings=settings)


def invoke(native, *args):
    return CliRunner().invoke(app, ["kick", "run", "--config", str(native.config), *args])


def test_native_kick_preserves_distinct_scheduled_profiles_and_needs_no_api(native):
    response = invoke(native, "--headless", "--json")
    assert response.exit_code == 0, response.output
    payload = json.loads(response.stdout)
    assert payload["interface_version"] == 1
    assert len(payload["data"]["runs"]) == 2
    assert [(settings.txn_usd_threshold, settings.txn_base_fee_cap_gwei, settings.txn_require_curve_quote)
            for settings, _, _ in native.captured] == [(100, 1, True), (50, 1, False)]
    assert [options["source_type"] for _, _, options in native.captured] == ["strategy", "fee_burner"]
    assert all(options["batch"] is False for _, _, options in native.captured)
    assert len(native.unlocks) == 1


@pytest.mark.parametrize("source,threshold,curve", [("strategy", 100, True), ("fee-burner", 50, False)])
def test_selected_profile_preserves_policy(native, source, threshold, curve):
    response = invoke(native, "--headless", "--json", "--source-type", source)
    assert response.exit_code == 0, response.output
    assert len(native.captured) == 1
    settings, _, options = native.captured[0]
    assert settings.txn_usd_threshold == threshold
    assert settings.txn_require_curve_quote is curve
    assert options["source_type"] == source.replace("-", "_")


def test_manual_fee_quote_and_value_overrides_reach_native_service(native):
    response = invoke(native, "--headless", "--json", "--source-type", "strategy",
                      "--min-usd-value", "175", "--max-base-fee-gwei", "0.5", "--no-require-curve")
    assert response.exit_code == 0, response.output
    settings, _, _ = native.captured[0]
    assert (settings.txn_usd_threshold, settings.txn_base_fee_cap_gwei, settings.txn_require_curve_quote) == (175, 0.5, False)
    # Applying an override must not mutate the shared base or other profile.
    assert native.settings.execution_profiles["strategy"].txn_usd_threshold == 100


def test_dry_run_never_unlocks_any_key(native):
    response = invoke(native, "--dry-run", "--json")
    assert response.exit_code == 0, response.output
    assert native.unlocks == []
    assert all(kwargs["signer"] is None and options["live"] is False for _, kwargs, options in native.captured)


def test_json_send_requires_deliberate_unattended_flag(native):
    response = invoke(native, "--json")
    assert response.exit_code == 2
    assert native.unlocks == []


@pytest.mark.parametrize("option", ["--min-usd-value", "--max-base-fee-gwei"])
def test_negative_policy_override_is_rejected_before_unlock(native, option):
    response = invoke(native, "--headless", option, "-1")
    assert response.exit_code == 2
    assert native.unlocks == []


def test_no_fill_override_requires_exact_pair_and_remains_disallowed_in_headless(native):
    for args in [("--allow-no-fill-retry",),
                 ("--allow-no-fill-retry", "--headless", "--auction", "0x" + "2" * 40, "--token", "0x" + "3" * 40)]:
        response = invoke(native, *args)
        assert response.exit_code == 2, response.output
    assert native.unlocks == []


def test_manual_guard_overrides_reach_native_planner(native):
    response = invoke(native, "--no-confirmation", "--json", "--source-type", "strategy",
                      "--allow-killed-gauge", "--allow-no-fill-retry",
                      "--auction", "0x" + "2" * 40, "--token", "0x" + "3" * 40)
    assert response.exit_code == 0, response.output
    _, _, options = native.captured[0]
    assert options["allow_killed_gauge"] and options["allow_no_fill_retry"]


def test_help_describes_local_execution_and_no_remote_control_plane_options():
    response = CliRunner().invoke(app, ["kick", "run", "--help"])
    assert response.exit_code == 0
    assert "--api-key" not in response.output
    assert "--api-base-url" not in response.output
    assert "--dry-run" in response.output


def test_missing_profile_is_explicit_instead_of_silently_inheriting_other_policy(native):
    native.settings.execution_profiles.pop("fee_burner")
    with pytest.raises(Exception, match="Missing explicit execution profile"):
        kick_cli._profile_settings(native.settings, "fee_burner")
